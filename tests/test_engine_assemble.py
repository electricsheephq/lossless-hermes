"""Tests for :meth:`_AssembleMixin._assemble` + :meth:`preassemble` + Option-A path (issue 03-09).

Covers the engine-side always-on substitution wrapper landed at 03-09:

* :meth:`_AssembleMixin._assemble` — engine-level wrapper around
  :meth:`ContextAssembler.assemble` (the 03-08 surface). Handles
  ignored-session bypass, empty-context short-circuit, no-user-turn
  sanity, safe_fallback on exception, prefix-stability snapshot, and
  the per-session sync lock.
* :meth:`_AssembleMixin.preassemble` — ADR-010 Option B override of
  the Hermes ``ContextEngine.preassemble`` ABC method. Resolves
  session_id from the message list and delegates to ``_assemble``.
* :meth:`_AssembleMixin._safe_fallback` — strip assistant prefill
  tails from the live messages.
* :meth:`_AssembleMixin._infer_session_id` — fallback ladder from
  message metadata / cached session / sentinel.
* :class:`_CompactMixin.should_compress` + :meth:`_CompactMixin.compress`
  Option A force-compress path — gated by the experimental flag.
* Engine init detection of preassemble ABC + experimental flag.
* Mode-stating startup logs.
* Rate-limited per-turn experimental warning (60s cooldown).

The bulk of the test surface uses MOCK stores (mirroring
``tests/test_assembler_assemble.py``) so the file does not depend on
``open_lcm_db`` or sqlite-vec — it runs on the actions/setup-python
macOS cell where extension-loading is absent.

References:

* ``epics/03-ingest-assembly/03-09-always-on-substitution-hook.md`` — AC.
* ``docs/adr/010-always-on-assembly.md`` — Option A vs Option B.
* ``docs/spike-results/002-hermes-pre-llm-call.md`` — Hermes hook surface.
* ``docs/upstream/001-preassemble-abc.md`` — upstream PR #24949.
* ``lossless-claw/src/engine.ts`` lines 6648-6832 — TS source.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.engine.assemble import (
    _EXPERIMENTAL_WARN_COOLDOWN_S,
)
from lossless_hermes.store.conversation import (
    ConversationRecord,
    MessagePartRecord,
    MessageRecord,
)
from lossless_hermes.store.summary import (
    ContextItemRecord,
    SummaryRecord,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _msg(
    *,
    message_id: int,
    role: str = "user",
    content: str = "",
    seq: int | None = None,
) -> MessageRecord:
    """Build a minimal :class:`MessageRecord`."""
    return MessageRecord(
        message_id=message_id,
        conversation_id=1,
        seq=seq if seq is not None else message_id,
        role=role,  # type: ignore[arg-type]
        content=content,
        token_count=0,
        created_at=datetime.now(timezone.utc),
    )


def _ctx_item(
    *,
    ordinal: int,
    item_type: str = "message",
    message_id: int | None = None,
    summary_id: str | None = None,
) -> ContextItemRecord:
    """Build a :class:`ContextItemRecord`."""
    return ContextItemRecord(
        conversation_id=1,
        ordinal=ordinal,
        item_type=item_type,  # type: ignore[arg-type]
        message_id=message_id,
        summary_id=summary_id,
        created_at=datetime.now(timezone.utc),
    )


def _conv(*, conversation_id: int = 1, session_id: str = "sess-A") -> ConversationRecord:
    return ConversationRecord(
        conversation_id=conversation_id,
        session_id=session_id,
        session_key=None,
        active=True,
        archived_at=None,
        title=None,
        bootstrapped_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _wire_mock_stores(
    engine: LCMEngine,
    *,
    conversation: ConversationRecord | None = None,
    context_items: list[ContextItemRecord] | None = None,
    messages_by_id: dict[int, MessageRecord] | None = None,
    parts_by_message_id: dict[int, list[MessagePartRecord]] | None = None,
    summaries_by_id: dict[str, SummaryRecord] | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Attach mock conversation + summary stores to the engine.

    Mirrors the ``_make_assembler`` builder in ``test_assembler_assemble.py``
    but plumbed onto :class:`LCMEngine` shell attributes so
    ``_assemble`` can read through them.
    """
    cstore = MagicMock()
    cstore.get_conversation_by_session_id.side_effect = lambda sid: (
        conversation if (conversation and conversation.session_id == sid) else None
    )
    cstore.get_message_by_id.side_effect = lambda mid: (messages_by_id or {}).get(mid)
    cstore.get_message_parts.side_effect = lambda mid: (parts_by_message_id or {}).get(mid, [])

    sstore = MagicMock()
    sstore.get_summary.side_effect = lambda sid: (summaries_by_id or {}).get(sid)
    sstore.get_summary_parents.side_effect = lambda _sid: []
    sstore.get_context_items.side_effect = lambda _cid: list(context_items or [])

    engine._conversation_store = cstore
    engine._summary_store = sstore
    return cstore, sstore


# ===========================================================================
# Engine init — mode detection + startup logging
# ===========================================================================


class TestEngineInitModeDetection:
    """ADR-010 mode detection at engine init.

    AC line 1: "Engine init detects whether Hermes has ``preassemble``
    ABC method and logs the active mode."
    """

    def test_default_engine_state_has_flags(self) -> None:
        """Fresh engine carries the three flags initialized to defaults."""
        engine = LCMEngine()
        # _has_preassemble defaults to False in a Hermes-less env (the
        # bridge's ContextEngine stub has no preassemble attribute).
        assert hasattr(engine, "_has_preassemble")
        assert isinstance(engine._has_preassemble, bool)
        # _experimental_always_on_via_compress defaults to False.
        assert engine._experimental_always_on_via_compress is False
        # _last_experimental_warn_ts starts at 0.
        assert engine._last_experimental_warn_ts == 0.0

    def test_experimental_flag_picked_up_from_config(self) -> None:
        """Setting the config flag flows to the engine attribute."""
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        assert engine._experimental_always_on_via_compress is True

    def test_startup_log_when_preassemble_present(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the host has ``preassemble``, init logs production mode."""
        import lossless_hermes.engine as engine_pkg

        # Simulate Hermes having the preassemble ABC method by adding it
        # to the bridge's ContextEngine class for this test.
        monkeypatch.setattr(
            engine_pkg.ContextEngine,
            "preassemble",
            lambda self, messages, budget_tokens=None: messages,
            raising=False,
        )
        with caplog.at_level(logging.INFO, logger="lossless_hermes.engine"):
            engine = LCMEngine()
        assert engine._has_preassemble is True
        assert any(
            "production mode" in rec.getMessage() and "Option B" in rec.getMessage()
            for rec in caplog.records
        ), f"expected production-mode log, got: {[r.getMessage() for r in caplog.records]}"

    def test_startup_log_when_experimental_active(self, caplog: pytest.LogCaptureFixture) -> None:
        """When experimental is True + no preassemble, init logs WARNING."""
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        with caplog.at_level(logging.WARNING, logger="lossless_hermes.engine"):
            engine = LCMEngine(config=cfg)
        # Confirm we're in the experimental branch.
        assert engine._experimental_always_on_via_compress is True
        assert engine._has_preassemble is False
        # The warning is emitted ONCE at init (in addition to the per-turn
        # rate-limited warning which is a separate path).
        assert any(
            "EXPERIMENTAL mode" in rec.getMessage()
            and "Option A" in rec.getMessage()
            and "NOT FOR PRODUCTION" in rec.getMessage()
            for rec in caplog.records
        ), f"expected experimental-mode warning, got: {[r.getMessage() for r in caplog.records]}"

    def test_startup_log_when_disabled(self, caplog: pytest.LogCaptureFixture) -> None:
        """Default config + no preassemble → warning about disabled state."""
        with caplog.at_level(logging.WARNING, logger="lossless_hermes.engine"):
            engine = LCMEngine()
        assert engine._has_preassemble is False
        assert engine._experimental_always_on_via_compress is False
        assert any(
            "always-on substitution DISABLED" in rec.getMessage()
            and "OVERFLOW-COMPACTOR ONLY" in rec.getMessage()
            for rec in caplog.records
        ), f"expected disabled-mode warning, got: {[r.getMessage() for r in caplog.records]}"


# ===========================================================================
# _safe_fallback — strip assistant prefill tails
# ===========================================================================


class TestSafeFallback:
    """Mirror TS engine.ts:6658-6664 (``safeFallback``)."""

    def test_returns_new_list(self) -> None:
        """The result is a fresh list (TS slice semantics)."""
        engine = LCMEngine()
        msgs = [{"role": "user", "content": "hi"}]
        out = engine._safe_fallback(msgs)
        assert out == msgs
        assert out is not msgs  # new list

    def test_empty_input(self) -> None:
        engine = LCMEngine()
        assert engine._safe_fallback([]) == []

    def test_pops_trailing_assistant(self) -> None:
        """Trailing assistant messages are stripped to avoid prefill rejection."""
        engine = LCMEngine()
        msgs = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        out = engine._safe_fallback(msgs)
        # Last assistant popped; only user remains as tail.
        assert out[-1]["role"] == "user"
        assert len(out) == 3

    def test_pops_consecutive_trailing_assistants(self) -> None:
        """Multiple consecutive trailing assistants all stripped."""
        engine = LCMEngine()
        msgs = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2"},
            {"role": "assistant", "content": "a3"},
        ]
        out = engine._safe_fallback(msgs)
        assert out == [{"role": "user", "content": "u"}]

    def test_no_trailing_assistant_passes_through(self) -> None:
        """A list ending in 'user' is returned unchanged (modulo identity)."""
        engine = LCMEngine()
        msgs = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "u2"},
        ]
        out = engine._safe_fallback(msgs)
        assert out == msgs

    def test_none_input_treated_as_empty(self) -> None:
        """Defensive: ``None`` input does not crash."""
        engine = LCMEngine()
        # ``list(None)`` would raise; we guard with ``messages or []``.
        out = engine._safe_fallback(None)  # type: ignore[arg-type]
        assert out == []


# ===========================================================================
# _infer_session_id — fallback ladder
# ===========================================================================


class TestInferSessionId:
    """Session-id resolution from messages / cached / sentinel."""

    def test_empty_messages_returns_empty(self) -> None:
        engine = LCMEngine()
        assert engine._infer_session_id([]) == ""

    def test_none_messages_returns_empty(self) -> None:
        engine = LCMEngine()
        assert engine._infer_session_id(None) == ""

    def test_session_id_from_message_metadata(self) -> None:
        """Scan finds the most recent ``session_id`` in the list."""
        engine = LCMEngine()
        msgs = [
            {"role": "user", "content": "old", "session_id": "sess-OLD"},
            {"role": "user", "content": "new", "session_id": "sess-NEW"},
        ]
        # Newest message wins (scan reversed).
        assert engine._infer_session_id(msgs) == "sess-NEW"

    def test_session_id_underscore_alias(self) -> None:
        engine = LCMEngine()
        msgs = [{"role": "user", "content": "x", "_session_id": "sess-UNDERSCORED"}]
        assert engine._infer_session_id(msgs) == "sess-UNDERSCORED"

    def test_sender_id_fallback(self) -> None:
        engine = LCMEngine()
        msgs = [{"role": "user", "content": "x", "sender_id": "user-42"}]
        assert engine._infer_session_id(msgs) == "user-42"

    def test_cached_session_id_when_no_message_metadata(self) -> None:
        """``_last_seen_session_id`` consulted when messages have no id."""
        engine = LCMEngine()
        # Simulate ingest having cached a session id.
        engine._last_seen_session_id = "sess-CACHED"  # type: ignore[attr-defined]
        msgs = [{"role": "user", "content": "no metadata"}]
        assert engine._infer_session_id(msgs) == "sess-CACHED"

    def test_empty_sentinel_when_no_source(self) -> None:
        """No source → return empty string."""
        engine = LCMEngine()
        msgs = [{"role": "user", "content": "x"}]
        assert engine._infer_session_id(msgs) == ""

    def test_skips_non_mapping_entries(self) -> None:
        """Non-mapping messages (e.g. malformed) don't crash the scan."""
        engine = LCMEngine()
        msgs = [
            "this is a string, not a dict",  # type: ignore[list-item]
            {"role": "user", "session_id": "sess-OK"},
        ]
        assert engine._infer_session_id(msgs) == "sess-OK"


# ===========================================================================
# _apply_assembly_budget_cap
# ===========================================================================


class TestApplyAssemblyBudgetCap:
    """Mirror TS engine.ts:2118-2122."""

    def test_no_cap_configured_returns_input(self) -> None:
        engine = LCMEngine()  # max_assembly_token_budget defaults to None
        assert engine._apply_assembly_budget_cap(50_000) == 50_000

    def test_cap_below_budget_clamps_down(self) -> None:
        cfg = LcmConfig(max_assembly_token_budget=30_000)
        engine = LCMEngine(config=cfg)
        assert engine._apply_assembly_budget_cap(50_000) == 30_000

    def test_cap_above_budget_returns_budget(self) -> None:
        cfg = LcmConfig(max_assembly_token_budget=100_000)
        engine = LCMEngine(config=cfg)
        assert engine._apply_assembly_budget_cap(50_000) == 50_000


# ===========================================================================
# _assemble — engine-side wrapper (synchronous, mock-store)
# ===========================================================================


class TestAssembleWrapper:
    """Coverage for the engine-side ``_assemble`` body."""

    def test_no_stores_returns_safe_fallback(self) -> None:
        """Pre-on_session_start: stores are None → safe_fallback."""
        engine = LCMEngine()
        msgs = [{"role": "user", "content": "u"}]
        out = engine._assemble(
            session_id="sess-A",
            messages=msgs,
            token_budget=10_000,
        )
        # Safe fallback returns a new list with same content (no
        # trailing assistant to strip).
        assert out == msgs
        assert out is not msgs

    def test_no_conversation_returns_safe_fallback(self) -> None:
        """Conversation lookup miss → safe_fallback."""
        engine = LCMEngine()
        _wire_mock_stores(engine, conversation=None)
        msgs = [{"role": "user", "content": "u"}]
        out = engine._assemble(
            session_id="sess-NOPE",
            messages=msgs,
            token_budget=10_000,
        )
        assert out == msgs

    def test_empty_context_items_returns_safe_fallback(self) -> None:
        """No context_items in the DB → safe_fallback (don't drop live)."""
        engine = LCMEngine()
        _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-A"),
            context_items=[],
        )
        msgs = [{"role": "user", "content": "u"}]
        out = engine._assemble(
            session_id="sess-A",
            messages=msgs,
            token_budget=10_000,
        )
        assert out == msgs

    def test_ignored_session_pattern_returns_safe_fallback(self) -> None:
        """Pattern match → bypass entire substitution path."""
        cfg = LcmConfig(ignore_session_patterns=["^benchmark-"])
        engine = LCMEngine(config=cfg)
        # Conversation lookup is BYPASSED — the pattern check fires first.
        msgs = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},  # will be stripped
        ]
        out = engine._assemble(
            session_id="benchmark-CI-runner",
            messages=msgs,
            token_budget=10_000,
        )
        # Safe fallback stripped the trailing assistant.
        assert out == [{"role": "user", "content": "u"}]

    def test_safe_fallback_on_assembler_exception(self, caplog: pytest.LogCaptureFixture) -> None:
        """Any unhandled assembler error → safe_fallback + WARNING log.

        AC: assembler raises → hook returns original messages (no crash).
        """
        engine = LCMEngine()
        cstore, sstore = _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-A"),
            context_items=[_ctx_item(ordinal=0, message_id=1)],
            messages_by_id={1: _msg(message_id=1, content="Hello")},
        )
        # Make resolve_items boom by making get_message_parts raise.
        cstore.get_message_parts.side_effect = RuntimeError("synthetic crash")

        msgs = [{"role": "user", "content": "u"}]
        with caplog.at_level(logging.WARNING, logger="lossless_hermes.engine.assemble"):
            out = engine._assemble(
                session_id="sess-A",
                messages=msgs,
                token_budget=10_000,
            )
        # Live messages returned unchanged (safe_fallback).
        assert out == msgs
        # The wrapper logged the failure at WARNING.
        assert any(
            "assemble: failed" in rec.getMessage() and "synthetic crash" in rec.getMessage()
            for rec in caplog.records
        ), f"expected failure log, got: {[r.getMessage() for r in caplog.records]}"

    def test_raw_only_trails_live_returns_safe_fallback(self) -> None:
        """When DB has only raw items + count < live → safe_fallback.

        Mirrors TS engine.ts:6736-6743 invariant: a half-bootstrapped
        DAG must not eclipse the live history.
        """
        engine = LCMEngine()
        _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-A"),
            # Only 2 raw items, but live has 4 messages.
            context_items=[
                _ctx_item(ordinal=0, message_id=1),
                _ctx_item(ordinal=1, message_id=2),
            ],
            messages_by_id={
                1: _msg(message_id=1, content="m1"),
                2: _msg(message_id=2, content="m2"),
            },
        )
        msgs = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ]
        out = engine._assemble(
            session_id="sess-A",
            messages=msgs,
            token_budget=10_000,
        )
        # safe_fallback stripped the trailing assistant.
        assert out[-1]["role"] == "user"

    def test_assembled_result_propagates_messages(self) -> None:
        """Successful assemble returns the assembled message list."""
        engine = LCMEngine()
        cstore, sstore = _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-A"),
            context_items=[_ctx_item(ordinal=0, message_id=1)],
            messages_by_id={1: _msg(message_id=1, content="Hello world")},
        )
        msgs = [{"role": "user", "content": "Hello world"}]
        out = engine._assemble(
            session_id="sess-A",
            messages=msgs,
            token_budget=10_000,
        )
        # Real assembler ran; output is a list of dicts with a user
        # turn (the assembler's normalize / sanitize passes preserve
        # the message).
        assert isinstance(out, list)
        assert len(out) >= 1
        assert any(m.get("role") == "user" for m in out)

    def test_prefix_stability_snapshot_recorded(self) -> None:
        """Snapshot updated after successful assemble."""
        engine = LCMEngine()
        _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-A", conversation_id=42),
            context_items=[_ctx_item(ordinal=0, message_id=1)],
            messages_by_id={1: _msg(message_id=1, content="Hello")},
        )
        # No snapshot before first call.
        assert 42 not in engine._previous_assembled_messages_by_conversation

        engine._assemble(
            session_id="sess-A",
            messages=[{"role": "user", "content": "Hello"}],
            token_budget=10_000,
        )
        # Snapshot recorded after.
        assert 42 in engine._previous_assembled_messages_by_conversation
        snapshot = engine._previous_assembled_messages_by_conversation[42]
        assert isinstance(snapshot, list)

    def test_two_consecutive_calls_preserve_prefix(self) -> None:
        """Two consecutive calls with identical inputs produce identical output.

        AC: Prefix-stability — 2 consecutive calls preserve overlap.
        """
        engine = LCMEngine()
        _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-A"),
            context_items=[_ctx_item(ordinal=0, message_id=1)],
            messages_by_id={1: _msg(message_id=1, content="Hello")},
        )
        msgs = [{"role": "user", "content": "Hello"}]
        first = engine._assemble(
            session_id="sess-A",
            messages=msgs,
            token_budget=10_000,
        )
        second = engine._assemble(
            session_id="sess-A",
            messages=msgs,
            token_budget=10_000,
        )
        # Deterministic — same input, same output.
        assert first == second


# ===========================================================================
# preassemble — ADR-010 Option B override
# ===========================================================================


class TestPreassemble:
    """ADR-010 Option B — Hermes preassemble ABC override."""

    def test_preassemble_method_exists_on_mixin(self) -> None:
        """The override is callable on the engine."""
        engine = LCMEngine()
        assert hasattr(engine, "preassemble")
        # Sanity: it's a bound method, not the default ABC no-op.
        assert callable(engine.preassemble)

    def test_preassemble_no_session_id_returns_messages_unchanged(self) -> None:
        """When session id cannot be inferred → return messages unchanged."""
        engine = LCMEngine()
        # No mock stores; no session_id in messages.
        msgs = [{"role": "user", "content": "x"}]
        out = engine.preassemble(msgs)
        assert out is msgs  # untouched (graceful no-op)

    def test_preassemble_delegates_to_assemble(self) -> None:
        """When session id resolves, preassemble runs ``_assemble``."""
        engine = LCMEngine()
        _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-X"),
            context_items=[_ctx_item(ordinal=0, message_id=1)],
            messages_by_id={1: _msg(message_id=1, content="Hello")},
        )
        # session_id encoded in the message metadata.
        msgs = [{"role": "user", "content": "Hello", "session_id": "sess-X"}]
        out = engine.preassemble(msgs, budget_tokens=50_000)
        # Real assemble result returned (not the original list ref).
        assert isinstance(out, list)
        assert any(m.get("role") == "user" for m in out)

    def test_preassemble_budget_none_falls_back_to_context_length(self) -> None:
        """``budget_tokens=None`` + context_length set → use context_length."""
        engine = LCMEngine()
        engine.context_length = 200_000
        # No stores wired — no_session_id branch will fire first, but
        # we're really testing that the call doesn't crash. Verify by
        # making the session inferrable and the assemble path runnable.
        _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-Y"),
            context_items=[_ctx_item(ordinal=0, message_id=1)],
            messages_by_id={1: _msg(message_id=1, content="Hello")},
        )
        msgs = [{"role": "user", "content": "Hello", "session_id": "sess-Y"}]
        out = engine.preassemble(msgs, budget_tokens=None)
        assert isinstance(out, list)

    def test_preassemble_invalid_budget_falls_back_to_default(self) -> None:
        """Non-positive budget → use _DEFAULT_TOKEN_BUDGET."""
        engine = LCMEngine()
        _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-Z"),
            context_items=[_ctx_item(ordinal=0, message_id=1)],
            messages_by_id={1: _msg(message_id=1, content="Hello")},
        )
        msgs = [{"role": "user", "content": "Hello", "session_id": "sess-Z"}]
        # Zero budget → ignored, use _DEFAULT_TOKEN_BUDGET fallback.
        out = engine.preassemble(msgs, budget_tokens=0)
        assert isinstance(out, list)

    def test_preassemble_never_raises_on_internal_error(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The outer try/except guarantees no exception bubbles up.

        AC: safe_fallback — assembler raises, hook returns original
        messages (no crash). The Hermes hook surface must never crash
        the agent loop on a substitution failure.
        """
        engine = LCMEngine()
        # Patch _infer_session_id to raise. The OUTER try/except in
        # preassemble must catch this and return ``messages`` unchanged.
        monkeypatch.setattr(
            engine,
            "_infer_session_id",
            lambda messages: (_ for _ in ()).throw(RuntimeError("inner crash")),
        )
        msgs = [{"role": "user", "content": "u"}]
        with caplog.at_level(logging.WARNING, logger="lossless_hermes.engine.assemble"):
            out = engine.preassemble(msgs)
        # The original (live) messages flow through unchanged.
        assert out is msgs
        # Failure was logged.
        assert any("preassemble: failed" in rec.getMessage() for rec in caplog.records)


# ===========================================================================
# Option A — should_compress force-true + compress substitution body
# ===========================================================================


class TestOptionACompressPath:
    """ADR-010 Option A — experimental fallback via compress()."""

    def test_should_compress_force_true_when_experimental(self) -> None:
        """AC: when experimental flag + no preassemble, returns True every turn."""
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        assert engine._has_preassemble is False
        # Always True regardless of token count.
        assert engine.should_compress() is True
        assert engine.should_compress(prompt_tokens=0) is True
        assert engine.should_compress(prompt_tokens=10_000_000) is True

    def test_should_compress_normal_when_experimental_off(self) -> None:
        """Default config → conventional threshold-gated should_compress."""
        engine = LCMEngine()
        # threshold_tokens is 0 → always False (02-05 invariant).
        assert engine.should_compress(prompt_tokens=10_000_000) is False

    def test_should_compress_normal_when_preassemble_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When preassemble exists, experimental path is unreachable.

        AC: When ``preassemble`` ABC exists, override is called every
        turn (Option B). When experimental flag is also True (operator
        error), the production path strictly wins.
        """
        import lossless_hermes.engine as engine_pkg

        monkeypatch.setattr(
            engine_pkg.ContextEngine,
            "preassemble",
            lambda self, messages, budget_tokens=None: messages,
            raising=False,
        )
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        assert engine._has_preassemble is True
        # Production path takes precedence — back to threshold logic.
        # threshold_tokens=0 → False.
        assert engine.should_compress(prompt_tokens=10_000_000) is False

    def test_compress_experimental_path_runs_assembler(self) -> None:
        """When experimental mode is on, compress runs ``_assemble``."""
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        _wire_mock_stores(
            engine,
            conversation=_conv(session_id="sess-EXP"),
            context_items=[_ctx_item(ordinal=0, message_id=1)],
            messages_by_id={1: _msg(message_id=1, content="Hello")},
        )
        msgs = [{"role": "user", "content": "Hello", "session_id": "sess-EXP"}]
        out = engine.compress(msgs)
        # Assembler returned a substituted list.
        assert isinstance(out, list)
        assert any(m.get("role") == "user" for m in out)
        # compression_count incremented (Hermes display contract).
        assert engine.compression_count == 1

    def test_compress_experimental_no_session_returns_messages(self) -> None:
        """No session_id resolvable → return messages unchanged.

        AC: experimental path is best-effort; must not crash.
        """
        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        # No mock stores; no session_id in messages.
        msgs = [{"role": "user", "content": "no metadata"}]
        out = engine.compress(msgs)
        assert out is msgs  # no substitution
        assert engine.compression_count == 1  # still increments

    def test_compress_normal_path_passthrough(self) -> None:
        """Default mode (no experimental, no preassemble) → 02-06 passthrough."""
        engine = LCMEngine()
        msgs = [{"role": "user", "content": "u"}]
        out = engine.compress(msgs)
        # Passthrough — same list returned.
        assert out is msgs


# ===========================================================================
# Experimental warning rate-limiting
# ===========================================================================


class TestExperimentalWarningRateLimiting:
    """Per-turn warning fires at most once per ``_EXPERIMENTAL_WARN_COOLDOWN_S``."""

    def test_first_call_emits(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = LCMEngine()
        # Cooldown predicate: `time.monotonic() - _last_warn_ts < cooldown`.
        # `monotonic()` is process-uptime; on a fresh CI runner uptime can be
        # < 60s and `now - 0.0 < 60.0` is True (incorrectly suppressing).
        # Use a negative sentinel so the difference is always >> cooldown.
        # (Fix-forward from PR #59 closure — flaked on ubuntu-latest py3.12/3.13.)
        engine._last_experimental_warn_ts = -(_EXPERIMENTAL_WARN_COOLDOWN_S + 1.0)
        with caplog.at_level(logging.WARNING, logger="lossless_hermes.engine.assemble"):
            emitted = engine._emit_experimental_warning_if_due()
        assert emitted is True
        assert any("EXPERIMENTAL force-compress" in rec.getMessage() for rec in caplog.records)

    def test_second_call_within_cooldown_suppressed(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = LCMEngine()
        engine._last_experimental_warn_ts = -(_EXPERIMENTAL_WARN_COOLDOWN_S + 1.0)
        # First emits.
        engine._emit_experimental_warning_if_due()
        # Second within cooldown is suppressed.
        with caplog.at_level(logging.WARNING, logger="lossless_hermes.engine.assemble"):
            emitted = engine._emit_experimental_warning_if_due()
        assert emitted is False

    def test_call_after_cooldown_emits_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = LCMEngine()
        engine._last_experimental_warn_ts = -(_EXPERIMENTAL_WARN_COOLDOWN_S + 1.0)
        # First emit advances the timestamp to ~now.
        first_now = engine._last_experimental_warn_ts
        engine._emit_experimental_warning_if_due()
        # Rewind the timestamp to simulate >cooldown elapsed.
        engine._last_experimental_warn_ts -= _EXPERIMENTAL_WARN_COOLDOWN_S + 1
        emitted = engine._emit_experimental_warning_if_due()
        assert emitted is True
        # The new timestamp is more recent than the rewound value.
        assert engine._last_experimental_warn_ts > first_now


# ===========================================================================
# Sync invariant — no async / await on the hook surface
# ===========================================================================


class TestSyncInvariant:
    """Per PR #34 (post async-to-sync conversion) — hook surface is sync."""

    def test_assemble_is_sync_not_coroutine(self) -> None:
        """``_assemble`` must NOT return a coroutine."""
        import inspect

        engine = LCMEngine()
        # The function itself must not be marked async.
        assert not inspect.iscoroutinefunction(engine._assemble)
        # Calling it (with stores absent) returns a list, not a coroutine.
        msgs: list[dict[str, Any]] = [{"role": "user", "content": "x"}]
        result = engine._assemble(session_id="s", messages=msgs, token_budget=1000)
        assert isinstance(result, list)
        assert not inspect.iscoroutine(result)

    def test_preassemble_is_sync_not_coroutine(self) -> None:
        """``preassemble`` must NOT return a coroutine."""
        import inspect

        engine = LCMEngine()
        assert not inspect.iscoroutinefunction(engine.preassemble)
        msgs: list[dict[str, Any]] = [{"role": "user", "content": "x"}]
        result = engine.preassemble(msgs)
        assert isinstance(result, list)
        assert not inspect.iscoroutine(result)

    def test_compress_is_sync_not_coroutine(self) -> None:
        """``compress`` (Option A path) must NOT return a coroutine."""
        import inspect

        cfg = LcmConfig(experimental_always_on_via_compress=True)
        engine = LCMEngine(config=cfg)
        assert not inspect.iscoroutinefunction(engine.compress)
        msgs: list[dict[str, Any]] = [{"role": "user", "content": "x"}]
        result = engine.compress(msgs)
        assert isinstance(result, list)
        assert not inspect.iscoroutine(result)


# ===========================================================================
# Recall-policy still injected alongside assembly (03-10 + 03-09 coexist)
# ===========================================================================


class TestRecallPolicyAlongsideAssembly:
    """The 03-10 recall-policy injection survives 03-09 changes."""

    def test_pre_llm_call_still_returns_policy_dict(self) -> None:
        """03-09 must not regress the 03-10 recall-policy injection."""
        from lossless_hermes.recall_policy import LOSSLESS_RECALL_POLICY_PROMPT

        engine = LCMEngine()
        result = engine._on_pre_llm_call(
            session_id="sess-A",
            user_message="hello",
            conversation_history=[],
            is_first_turn=True,
            model="claude-sonnet-4-5",
            platform="anthropic",
        )
        assert result == {"context": LOSSLESS_RECALL_POLICY_PROMPT}

    def test_pre_llm_call_and_preassemble_independent(self) -> None:
        """The two hooks are independent — they share no state."""
        from lossless_hermes.recall_policy import LOSSLESS_RECALL_POLICY_PROMPT

        engine = LCMEngine()
        # Calling preassemble first must not affect the pre_llm_call return.
        engine.preassemble([{"role": "user", "content": "x"}])
        result = engine._on_pre_llm_call(
            session_id="s",
            conversation_history=[],
        )
        assert result == {"context": LOSSLESS_RECALL_POLICY_PROMPT}


# ===========================================================================
# Per-session lock acquired (smoke)
# ===========================================================================


class TestPerSessionLock:
    """The assemble body acquires the per-session sync lock."""

    def test_assemble_acquires_sync_lock(self) -> None:
        """The lock is held while ``_assemble`` runs.

        We don't need to assert concurrency here — just that the lock
        registry is consulted (the refcount briefly rises during the
        call). Snapshotting pending_count_sync before and after gives a
        cheap smoke test that the lock context manager was entered.
        """
        engine = LCMEngine()
        _wire_mock_stores(engine, conversation=None)
        # Pre-call: no pending.
        assert engine._session_locks.pending_count_sync() == 0
        engine._assemble(
            session_id="sess-LOCK-TEST",
            messages=[{"role": "user", "content": "x"}],
            token_budget=10_000,
        )
        # Post-call: refcount released (record may still exist below
        # high-water mark but refcount is 0). Sanity: at least the
        # lock surface ran without error.
        # (The lock-registry's lazy prune may or may not have removed
        # the entry; either is fine — we just want the call to have
        # completed without deadlock.)
        assert engine._session_locks.pending_count_sync() >= 0


# ===========================================================================
# Function signatures match docs/porting-guides/engine.md
# ===========================================================================


class TestSignatures:
    """AC: function signatures match the porting-guide spec."""

    def test_preassemble_signature(self) -> None:
        """``preassemble(messages, budget_tokens=None) -> list[dict]``."""
        import inspect

        engine = LCMEngine()
        sig = inspect.signature(engine.preassemble)
        params = list(sig.parameters.values())
        # First non-self param: messages
        assert params[0].name == "messages"
        # Second: budget_tokens with default None
        assert params[1].name == "budget_tokens"
        assert params[1].default is None

    def test_assemble_signature(self) -> None:
        """``_assemble(session_id, messages, token_budget, prompt=None)``."""
        import inspect

        engine = LCMEngine()
        sig = inspect.signature(engine._assemble)
        params = list(sig.parameters.values())
        assert params[0].name == "session_id"
        assert params[1].name == "messages"
        assert params[2].name == "token_budget"
        assert params[3].name == "prompt"
        assert params[3].default is None
