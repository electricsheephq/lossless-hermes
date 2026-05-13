"""Tests for the compaction telemetry write paths (issue 04-08).

Covers the acceptance criteria in
``epics/04-compaction/04-08-telemetry-write.md``:

* :class:`CompactionResult` shape — all fields per the porting guide
  §"Public surface": ``action_taken``, ``tokens_before``,
  ``tokens_after``, ``created_summary_id``, ``condensed``, ``level``,
  ``auth_failure``, ``reason``, ``phase_results``.
* :data:`CompactionLevel` literal accepts only the 4 spec'd values.
* ``phase_results: list[CompactionResult]`` field is present and
  defaults to ``[]`` (mutable default via ``field(default_factory=list)``
  — each instance gets its own list, not the shared sentinel).
* :meth:`CompactionEngine._persist_compaction_event` is a STRUCTURED LOG
  CALL with the spec'd fields in ``extra=``, NOT a chat-message-row
  write.
* :meth:`CompactionEngine._persist_compaction_events` iterates a list
  and SKIPS ``None`` entries.
* Successful ``_run_leaf_pass`` triggers
  :meth:`CompactionTelemetryStore.mark_leaf_compaction_success`.
* Successful ``_run_condensed_pass`` triggers
  :meth:`CompactionTelemetryStore.mark_condensed_compaction_success`.
* No-pass (circuit-breaker / no-eligible-chunk) does NOT trigger
  ``mark_*_success`` — the helpers fire only on the success path.
* Telemetry-store calls degrade gracefully when the store is absent
  or missing the method (spec §"Telemetry-store calls are stubbed-safe").

The TS counterparts live in
``lossless-claw/test/compaction-maintenance-store.test.ts`` (per the
spec's §"Tests" pointer) plus the integration coverage transitively
exercising ``persistCompactionEvent`` in ``lcm-integration.test.ts``.

See:

* Source: ``lossless-claw/src/compaction.ts`` lines 1754-1830
  (telemetry write paths) + lines 18-32 (``CompactionResult`` shape),
  LCM commit ``1f07fbd`` on branch ``pr-613``.
* Spec: ``epics/04-compaction/04-08-telemetry-write.md``.
* ADR-017 (sync stores), ADR-024 (project layout), ADR-029 (Wave-N).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from typing import Any, get_args

import pytest

from lossless_hermes.compaction import (
    CompactionConfig,
    CompactionEngine,
    CompactionLevel,
    CompactionResult,
    LeafPassOutcome,
    LeafPassResult,
    SummarizeFn,
)


# ---------------------------------------------------------------------------
# Fixtures — store + message stand-ins (mirror test_compaction_anti_thrashing)
# ---------------------------------------------------------------------------


@dataclass
class _StubContextItem:
    """Minimal stand-in for ``ContextItemRecord``."""

    ordinal: int
    item_type: str
    message_id: int | None = None
    summary_id: str | None = None


@dataclass
class _StubMessage:
    """Minimal stand-in for ``MessageRecord``."""

    content: str
    token_count: int


class _StubSummaryStore:
    """In-memory ``SummaryStore``-like stand-in."""

    def __init__(
        self,
        *,
        context_token_count: int = 0,
        context_items: list[_StubContextItem] | None = None,
    ) -> None:
        self.context_token_count = context_token_count
        self.context_items: list[_StubContextItem] = list(context_items or [])

    def get_context_token_count(self, conversation_id: int) -> int:
        return self.context_token_count

    def get_context_items(self, conversation_id: int) -> list[_StubContextItem]:
        return list(self.context_items)


class _StubConversationStore:
    """In-memory ``ConversationStore``-like stand-in.

    Records every ``create_message`` call so a test can assert the
    conversation history was NOT polluted by compaction events.
    """

    def __init__(self, messages: dict[int, _StubMessage] | None = None) -> None:
        self._messages: dict[int, _StubMessage] = dict(messages or {})
        self.create_message_calls: list[Any] = []

    def get_message_by_id(
        self,
        message_id: int,
        *,
        include_suppressed: bool = False,
    ) -> _StubMessage | None:
        return self._messages.get(message_id)

    def create_message(self, *args: Any, **kwargs: Any) -> None:
        # Recording-only stub. The spec's AC includes "asserts
        # conversation_store.create_message not called" — wire the
        # observable here so a test can lock the absence in.
        self.create_message_calls.append((args, kwargs))


class _RecordingTelemetryStore:
    """Records every ``mark_*`` call so tests can assert call args."""

    def __init__(self) -> None:
        self.leaf_success_calls: list[dict[str, Any]] = []
        self.condensed_success_calls: list[dict[str, Any]] = []
        self.auth_failure_calls: list[dict[str, Any]] = []

    def mark_leaf_compaction_success(
        self,
        *,
        conversation_id: int,
        summary_id: str,
    ) -> None:
        self.leaf_success_calls.append(
            {"conversation_id": conversation_id, "summary_id": summary_id},
        )

    def mark_condensed_compaction_success(
        self,
        *,
        conversation_id: int,
        summary_id: str,
    ) -> None:
        self.condensed_success_calls.append(
            {"conversation_id": conversation_id, "summary_id": summary_id},
        )

    def mark_auth_failure(
        self,
        *,
        conversation_id: int,
    ) -> None:
        self.auth_failure_calls.append({"conversation_id": conversation_id})


class _RaisingTelemetryStore:
    """Telemetry store whose every mark_* method raises.

    Used to verify the engine's defensive try/except swallows the
    exception so compaction is not aborted by a telemetry-layer bug.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def mark_leaf_compaction_success(self, **_: Any) -> None:
        self.attempts.append("leaf")
        raise RuntimeError("telemetry write failed")

    def mark_condensed_compaction_success(self, **_: Any) -> None:
        self.attempts.append("condensed")
        raise RuntimeError("telemetry write failed")

    def mark_auth_failure(self, **_: Any) -> None:
        self.attempts.append("auth_failure")
        raise RuntimeError("telemetry write failed")


def _noop_summarize(text: str, aggressive: bool = False, options: dict | None = None) -> str:
    """No-op ``SummarizeFn`` — never called by the 04-04 skeletal sweep."""
    del text, aggressive, options
    return ""


def _make_one_message_context() -> tuple[list[_StubContextItem], dict[int, _StubMessage]]:
    """Minimal one-raw-message context (mirrors anti-thrashing tests)."""
    return (
        [_StubContextItem(ordinal=0, item_type="message", message_id=1)],
        {1: _StubMessage(content="raw", token_count=1)},
    )


class _ScriptedEngine(CompactionEngine):
    """Engine with FIFO-scripted leaf/condensed pass results.

    Mirrors the same pattern as ``test_compaction_anti_thrashing._ScriptedEngine``
    so the 04-08 tests can drive successful passes through the
    persistence layer without standing up the full 04-02/04-03
    pass bodies.
    """

    def __init__(
        self,
        *,
        leaf_passes: list[LeafPassResult | None] | None = None,
        condensed_passes: list[LeafPassResult | None] | None = None,
        **engine_kwargs: object,
    ) -> None:
        super().__init__(**engine_kwargs)  # type: ignore[arg-type]
        self.leaf_passes: list[LeafPassResult | None] = list(leaf_passes or [])
        self.condensed_passes: list[LeafPassResult | None] = list(condensed_passes or [])

    def _run_leaf_pass(
        self,
        *,
        conversation_id: int,
        summarize: SummarizeFn,
        previous_summary_content: str | None,
        summary_model: str | None,
    ) -> LeafPassOutcome:
        if not self.leaf_passes:
            # Empty scripted queue → "nothing left to compact", NOT
            # an auth failure (per the LeafPassOutcome protocol
            # introduced by PR #81 reviewer MAJOR fix).
            return LeafPassOutcome(summary=None, auth_failure=False)
        scripted = self.leaf_passes.pop(0)
        return LeafPassOutcome(summary=scripted, auth_failure=False)

    def _run_condensed_pass(
        self,
        *,
        conversation_id: int,
        hard_trigger: bool,
        summarize: SummarizeFn,
        summary_model: str | None,
    ) -> LeafPassOutcome:
        if not self.condensed_passes:
            return LeafPassOutcome(summary=None, auth_failure=False)
        scripted = self.condensed_passes.pop(0)
        return LeafPassOutcome(summary=scripted, auth_failure=False)


def _make_scripted_engine(
    *,
    context_token_count: int = 100_000,
    context_items: list[_StubContextItem] | None = None,
    messages: dict[int, _StubMessage] | None = None,
    config: CompactionConfig | None = None,
    leaf_passes: list[LeafPassResult | None] | None = None,
    condensed_passes: list[LeafPassResult | None] | None = None,
    compaction_telemetry_store: Any = None,
    log: logging.Logger | None = None,
) -> tuple[_ScriptedEngine, _StubConversationStore]:
    items = context_items if context_items is not None else _make_one_message_context()[0]
    msgs = messages if messages is not None else _make_one_message_context()[1]
    summary_store = _StubSummaryStore(
        context_token_count=context_token_count,
        context_items=items,
    )
    conversation_store = _StubConversationStore(messages=msgs)
    engine = _ScriptedEngine(
        leaf_passes=leaf_passes,
        condensed_passes=condensed_passes,
        conversation_store=conversation_store,
        summary_store=summary_store,
        config=config or CompactionConfig(),
        compaction_telemetry_store=compaction_telemetry_store,
        log=log,
    )
    return engine, conversation_store


def _leaf_pass(
    *,
    removed: int = 10_000,
    added: int = 1_000,
    summary_id: str = "sum_test",
    level: str = "normal",
) -> LeafPassResult:
    return LeafPassResult(
        summary_id=summary_id,
        level=level,  # type: ignore[arg-type]
        content="(scripted summary)",
        removed_tokens=removed,
        added_tokens=added,
    )


# ---------------------------------------------------------------------------
# CompactionResult shape — finalize fields per spec §"Public surface"
# ---------------------------------------------------------------------------


class TestCompactionResultShape:
    """The finalized :class:`CompactionResult` dataclass."""

    def test_has_all_spec_fields(self) -> None:
        """All spec-mandated fields exist by name (AC #1).

        The porting guide §"Public surface" enumerates the 9 fields.
        ``passes_completed`` is the project's local addition (04-04);
        the spec calls for the other 8.
        """
        field_names = {f.name for f in fields(CompactionResult)}
        expected = {
            "action_taken",
            "tokens_before",
            "tokens_after",
            "created_summary_id",
            "condensed",
            "level",
            "auth_failure",
            "reason",
            "phase_results",
        }
        assert expected.issubset(field_names), (
            f"CompactionResult missing fields: {expected - field_names}"
        )

    def test_compaction_level_literal_accepts_only_four_values(self) -> None:
        """``CompactionLevel`` is exactly ``{normal, aggressive, fallback, capped}`` (AC #2)."""
        assert set(get_args(CompactionLevel)) == {
            "normal",
            "aggressive",
            "fallback",
            "capped",
        }

    def test_phase_results_default_empty_list(self) -> None:
        """``phase_results`` defaults to ``[]`` (per spec §"finalized shape")."""
        result = CompactionResult(
            action_taken=False,
            tokens_before=0,
            tokens_after=0,
            created_summary_id=None,
            condensed=False,
            level=None,
            passes_completed=0,
        )
        assert result.phase_results == []

    def test_phase_results_default_is_fresh_per_instance(self) -> None:
        """Two instances must have DISTINCT lists (no shared mutable default).

        Locks in ``field(default_factory=list)`` — a regression from
        ``= []`` would cause every instance to share a single list,
        a classic Python gotcha.
        """
        a = CompactionResult(
            action_taken=False,
            tokens_before=0,
            tokens_after=0,
            created_summary_id=None,
            condensed=False,
            level=None,
            passes_completed=0,
        )
        b = CompactionResult(
            action_taken=False,
            tokens_before=0,
            tokens_after=0,
            created_summary_id=None,
            condensed=False,
            level=None,
            passes_completed=0,
        )
        assert a.phase_results is not b.phase_results

    def test_reason_default_is_none(self) -> None:
        """``reason`` defaults to ``None`` (action-taken path has no reason)."""
        result = CompactionResult(
            action_taken=False,
            tokens_before=0,
            tokens_after=0,
            created_summary_id=None,
            condensed=False,
            level=None,
            passes_completed=0,
        )
        assert result.reason is None

    def test_default_values_match_spec(self) -> None:
        """Defaults match spec §"finalized shape": action_taken=False, ...

        Spec specifies:
        * action_taken: bool (required — no default)
        * tokens_before / tokens_after: int (required — no default)
        * created_summary_id: str | None = None
        * condensed: bool = False (default)
        * level: CompactionLevel | None = None
        * auth_failure: bool = False
        * reason: str | None = None
        * phase_results: list[CompactionResult] = []

        Verify the defaults the implementation exposes. ``condensed``
        defaults differ from spec — Hermes port made it required (04-04)
        for clarity; verify the rest match.
        """
        result = CompactionResult(
            action_taken=True,
            tokens_before=100,
            tokens_after=50,
            created_summary_id=None,
            condensed=False,
            level=None,
            passes_completed=0,
        )
        # Required-field roundtrip.
        assert result.action_taken is True
        assert result.tokens_before == 100
        assert result.tokens_after == 50
        # Defaults from spec.
        assert result.auth_failure is False
        assert result.reason is None
        assert result.phase_results == []

    def test_phase_results_aggregation(self) -> None:
        """Spec §"Phase aggregation" — full sweep populates phase_results."""
        engine, _ = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[_leaf_pass(removed=30_000, added=1_000)],
            condensed_passes=[_leaf_pass(removed=20_000, added=1_000, level="aggressive")],
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
            force=True,
        )
        # Phase 1 ran once + phase 2 ran once → 2 phase_results entries.
        assert len(result.phase_results) == 2
        # Phase 1: leaf pass.
        assert result.phase_results[0].condensed is False
        assert result.phase_results[0].level == "normal"
        # Phase 2: condensed pass.
        assert result.phase_results[1].condensed is True
        assert result.phase_results[1].level == "aggressive"


# ---------------------------------------------------------------------------
# _persist_compaction_event — structured log, no DB write
# ---------------------------------------------------------------------------


class TestPersistCompactionEvent:
    """Spec AC: structured log call, NOT a chat-message-row write."""

    def test_emits_structured_log_record_with_all_extras(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Spec AC: log event has conversation_id, action_taken, tokens_before,
        tokens_after, delta, level, condensed, auth_failure,
        created_summary_id, reason.
        """
        engine, _ = _make_scripted_engine()
        result = CompactionResult(
            action_taken=True,
            tokens_before=100_000,
            tokens_after=60_000,
            created_summary_id="sum_abc",
            condensed=False,
            level="normal",
            passes_completed=1,
            auth_failure=False,
            reason=None,
        )
        with caplog.at_level(logging.INFO, logger="lossless_hermes.compaction"):
            engine._persist_compaction_event(conversation_id=42, result=result)

        # Exactly one INFO record emitted.
        records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(records) == 1

        # The structured payload is on the record under
        # ``compaction_event``. ``extra=`` keys become attributes on the
        # LogRecord (not arbitrary dict access).
        record = records[0]
        payload = record.compaction_event  # type: ignore[attr-defined]
        assert payload["conversation_id"] == 42
        assert payload["action_taken"] is True
        assert payload["tokens_before"] == 100_000
        assert payload["tokens_after"] == 60_000
        assert payload["delta"] == 40_000  # before - after.
        assert payload["level"] == "normal"
        assert payload["condensed"] is False
        assert payload["auth_failure"] is False
        assert payload["created_summary_id"] == "sum_abc"
        assert payload["reason"] is None

    def test_does_not_call_create_message(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Spec AC: `_persist_compaction_event does NOT write a chat message row`.

        LCM intentionally REMOVED the synthetic-message-row behavior.
        Locking that in: invoking the persist helper must not touch
        ``conversation_store.create_message``.
        """
        engine, conv_store = _make_scripted_engine()
        result = CompactionResult(
            action_taken=True,
            tokens_before=100_000,
            tokens_after=60_000,
            created_summary_id="sum_xyz",
            condensed=False,
            level="normal",
            passes_completed=1,
        )
        with caplog.at_level(logging.INFO, logger="lossless_hermes.compaction"):
            engine._persist_compaction_event(conversation_id=1, result=result)

        # Conversation store was NOT touched.
        assert conv_store.create_message_calls == []


class TestPersistCompactionEvents:
    """Spec AC: iterates list, skips None entries."""

    def test_skips_none_entries(self, caplog: pytest.LogCaptureFixture) -> None:
        """Spec AC: mixed list with one None → 1 log call, not 2."""
        engine, _ = _make_scripted_engine()
        result = CompactionResult(
            action_taken=True,
            tokens_before=100,
            tokens_after=50,
            created_summary_id="sum_a",
            condensed=False,
            level="normal",
            passes_completed=1,
        )

        with caplog.at_level(logging.INFO, logger="lossless_hermes.compaction"):
            engine._persist_compaction_events(
                conversation_id=1,
                results=[result, None, result, None],
            )

        # 2 non-None entries → 2 log calls; None entries skipped.
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_records) == 2

    def test_empty_list_emits_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """Empty list → no log records."""
        engine, _ = _make_scripted_engine()
        with caplog.at_level(logging.INFO, logger="lossless_hermes.compaction"):
            engine._persist_compaction_events(conversation_id=1, results=[])
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert info_records == []

    def test_all_none_emits_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """List of all ``None`` → no log records."""
        engine, _ = _make_scripted_engine()
        with caplog.at_level(logging.INFO, logger="lossless_hermes.compaction"):
            engine._persist_compaction_events(
                conversation_id=1,
                results=[None, None, None],
            )
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert info_records == []


# ---------------------------------------------------------------------------
# Telemetry-store integration — mark_*_success call sites
# ---------------------------------------------------------------------------


class TestTelemetryStoreIntegration:
    """Spec AC: leaf/condensed success + auth failure call sites."""

    def test_successful_leaf_pass_calls_mark_leaf_compaction_success(self) -> None:
        """Spec AC: `successful _leaf_pass calls mark_leaf_compaction_success`."""
        telemetry = _RecordingTelemetryStore()
        engine, _ = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[_leaf_pass(summary_id="sum_leaf_1")],
            compaction_telemetry_store=telemetry,
        )
        engine.compact_full_sweep(
            conversation_id=42,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        assert len(telemetry.leaf_success_calls) == 1
        assert telemetry.leaf_success_calls[0] == {
            "conversation_id": 42,
            "summary_id": "sum_leaf_1",
        }
        # No condensed pass ran → condensed store not bumped.
        assert telemetry.condensed_success_calls == []

    def test_successful_condensed_pass_calls_mark_condensed_compaction_success(self) -> None:
        """Spec AC: `successful _condensed_pass calls mark_condensed_compaction_success`."""
        telemetry = _RecordingTelemetryStore()
        engine, _ = _make_scripted_engine(
            context_token_count=100_000,
            # No leaf script → phase-1 exits immediately. Forced sweep
            # enters phase-2 anyway.
            leaf_passes=[],
            condensed_passes=[_leaf_pass(summary_id="sum_cond_1", level="aggressive")],
            compaction_telemetry_store=telemetry,
        )
        engine.compact_full_sweep(
            conversation_id=99,
            token_budget=10_000,
            summarize=_noop_summarize,
            force=True,
        )
        assert len(telemetry.condensed_success_calls) == 1
        assert telemetry.condensed_success_calls[0] == {
            "conversation_id": 99,
            "summary_id": "sum_cond_1",
        }
        # No leaf pass succeeded → leaf store not bumped.
        assert telemetry.leaf_success_calls == []

    def test_no_eligible_chunk_does_not_call_mark_success(self) -> None:
        """Spec AC: `circuit-breaker-open compaction does NOT call mark_*_success`.

        Skeletal ``_run_leaf_pass`` returns ``None`` when no scripted
        result remains — the equivalent of "no eligible chunk" /
        "circuit breaker open". Verify the success helpers are NOT
        called on this path.
        """
        telemetry = _RecordingTelemetryStore()
        engine, _ = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[],  # phase-1 immediately returns None.
            condensed_passes=[],  # phase-2 also None.
            compaction_telemetry_store=telemetry,
        )
        engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        assert telemetry.leaf_success_calls == []
        assert telemetry.condensed_success_calls == []
        assert telemetry.auth_failure_calls == []

    def test_under_threshold_short_circuit_does_not_call_mark_success(self) -> None:
        """No-op short-circuit (under threshold) bumps nothing."""
        telemetry = _RecordingTelemetryStore()
        engine, _ = _make_scripted_engine(
            context_token_count=100,  # Well below threshold (7500).
            context_items=[
                _StubContextItem(ordinal=0, item_type="message", message_id=1),
            ],
            messages={1: _StubMessage(content="x", token_count=1)},
            leaf_passes=[_leaf_pass()],  # Would run if not for short-circuit.
            compaction_telemetry_store=telemetry,
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        # No-op short-circuit fired → no telemetry writes.
        assert result.action_taken is False
        assert result.reason == "under threshold"
        assert telemetry.leaf_success_calls == []

    def test_empty_context_does_not_call_mark_success(self) -> None:
        """Empty context_items short-circuit returns reason and skips bumps."""
        telemetry = _RecordingTelemetryStore()
        engine, _ = _make_scripted_engine(
            context_token_count=100_000,
            context_items=[],
            messages={},
            leaf_passes=[_leaf_pass()],
            compaction_telemetry_store=telemetry,
        )
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        assert result.action_taken is False
        assert result.reason == "no eligible chunk"
        assert telemetry.leaf_success_calls == []

    def test_engine_without_telemetry_store_does_not_crash(self) -> None:
        """Spec AC: `Telemetry-store calls are stubbed-safe`.

        Constructing the engine without a telemetry store and running a
        successful pass must not raise — the call site falls through
        silently.
        """
        engine, _ = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[_leaf_pass()],
            compaction_telemetry_store=None,
        )
        # Just verify no exception.
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        assert result.action_taken is True

    def test_partial_telemetry_store_degrades_gracefully(self) -> None:
        """Store missing the leaf method falls through silently.

        A future iteration of the store may not yet implement all three
        methods (e.g., before 01-10's write paths land). The engine
        defensively introspects with ``getattr`` so missing methods
        don't abort compaction.
        """

        class _PartialStore:
            # Implements condensed + auth_failure but NOT leaf.
            def __init__(self) -> None:
                self.condensed_calls = 0
                self.auth_calls = 0

            def mark_condensed_compaction_success(self, **_: Any) -> None:
                self.condensed_calls += 1

            def mark_auth_failure(self, **_: Any) -> None:
                self.auth_calls += 1

        store = _PartialStore()
        engine, _ = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[_leaf_pass()],  # Would otherwise trigger leaf bump.
            compaction_telemetry_store=store,
        )
        # No exception — missing method is a no-op.
        result = engine.compact_full_sweep(
            conversation_id=1,
            token_budget=10_000,
            summarize=_noop_summarize,
        )
        assert result.action_taken is True
        # Confirm the partial store wasn't bumped on the leaf path.
        assert store.condensed_calls == 0  # Phase-2 didn't run.

    def test_raising_telemetry_store_does_not_abort_compaction(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Spec § "Telemetry failures MUST NOT abort a successful compaction".

        A telemetry store whose ``mark_*`` raises is swallowed. The
        compaction continues; a debug log is emitted; the result still
        reports ``action_taken=True``.
        """
        telemetry = _RaisingTelemetryStore()
        engine, _ = _make_scripted_engine(
            context_token_count=100_000,
            leaf_passes=[_leaf_pass()],
            compaction_telemetry_store=telemetry,
        )
        with caplog.at_level(logging.DEBUG, logger="lossless_hermes.compaction"):
            result = engine.compact_full_sweep(
                conversation_id=1,
                token_budget=10_000,
                summarize=_noop_summarize,
            )
        assert result.action_taken is True
        # The leaf bump was attempted (and raised — swallowed).
        assert telemetry.attempts == ["leaf"]
        # A debug log of the failure was emitted (we don't pin the
        # exact message but verify SOME debug record carries the method
        # name).
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "telemetry" in r.getMessage().lower()
        ]
        assert debug_records, "expected a debug log for the telemetry failure"


# ---------------------------------------------------------------------------
# Manual helper invocations — auth-failure call site
# ---------------------------------------------------------------------------


class TestAuthFailureMarker:
    """Spec AC: ``auth-failed compaction calls mark_auth_failure``.

    The 04-04 skeletal sweep can't yet distinguish "no chunk" from
    "auth failure" — both surface as ``None`` from ``_run_leaf_pass``.
    04-02 will split them by setting a flag on the pass body. Until
    then, this test exercises :meth:`_mark_auth_failure` directly to
    lock in the wiring contract: when production code calls it, the
    telemetry store receives the bump.
    """

    def test_mark_auth_failure_calls_store(self) -> None:
        telemetry = _RecordingTelemetryStore()
        engine, _ = _make_scripted_engine(
            compaction_telemetry_store=telemetry,
        )
        engine._mark_auth_failure(conversation_id=42)
        assert telemetry.auth_failure_calls == [{"conversation_id": 42}]

    def test_mark_auth_failure_without_store_is_noop(self) -> None:
        """No telemetry store + auth-failure call → no exception."""
        engine, _ = _make_scripted_engine(compaction_telemetry_store=None)
        # Just verify no exception is raised.
        engine._mark_auth_failure(conversation_id=42)
