"""Direct unit tests for the ``lcm_synthesize_around`` dispatch adapter.

Issue [#164](https://github.com/electricsheephq/lossless-hermes/issues/164)
PR-2 added :func:`~lossless_hermes.tools._adapters._adapt_lcm_synthesize_around`
— the 8th and final #156 dispatch adapter, which brings tool-dispatch
coverage to 8/8 and closes #156.

Unlike the four PR-1 attribute-bag adapters, ``lcm_synthesize_around``'s
``SynthesizeAroundContext.build_llm_call`` is a *factory* (a
:class:`~..synthesize_around.BuildLlmCall`, ``() -> tuple[LlmCall, str]``).
The adapter builds that factory over the engine's #164 PR-2 summarizer
surface — :func:`~.._adapters._build_synthesizer_llm_call` wires the
synthesis dispatcher's async :class:`~..synthesis.dispatch.LlmCall` to
the engine's :class:`~..summarize.LcmSummarizer`.

The #156 regression test (``tests/test_dispatch_registry_coverage.py``)
verifies ``lcm_synthesize_around`` *dispatches*, but its (b) probe runs
the handler on a fresh fixtured engine with no conversation — the
handler returns "no conversation found" before reaching
``build_llm_call``. The #161 review established that an adapter must not
ship with zero direct test coverage; this file is that coverage,
mirroring ``tests/tools/test_compact_adapter.py``.

What this file covers
---------------------

* :func:`_build_synthesizer_llm_call`:
  - a configured engine yields ``(LlmCall, model_name)`` where the
    ``LlmCall`` is awaitable and produces an
    :class:`~..synthesis.dispatch.LlmCallResult` whose ``output`` is the
    summary and whose ``actual_model`` is the resolved primary candidate
    (the Wave-12 F8 audit-honesty contract);
  - a bare engine (no ``on_session_start``) raises ``RuntimeError``;
  - an engine with an empty candidate chain (no ``summary_model``)
    raises ``RuntimeError``.
* :func:`_adapt_lcm_synthesize_around`:
  - the engine-state-unset path degrades to a structured
    ``tool_result`` error, not an exception;
  - builds the :class:`SynthesizeAroundContext` correctly and
    dispatches end-to-end against an in-memory DB — a non-error
    ``tool_result`` plus a ``lcm_synthesis_cache`` row written;
  - the ``build_llm_call`` factory it wires raises (mis-configured
    summarizer) → the handler degrades to the structured "No
    summarization model resolved" tool-error.
* :class:`_SynthesizeAroundCtx` structurally satisfies
  :class:`SynthesizeAroundContext`.

Platform note
-------------

The ``_build_synthesizer_llm_call`` factory tests use a **bare**
:class:`LCMEngine` or an ``on_session_start``-fixtured one. The
fixtured / dispatch tests need ``open_lcm_db`` (sqlite-vec loads via
``enable_load_extension``); Apple's system CPython ships without
``--enable-loadable-sqlite-extensions`` and the engine hard-raises at
construction — so those carry the ``enable_load_extension`` skip
marker, mirroring ``tests/tools/test_compact_adapter.py``. The
bare-engine factory-guard tests run on every platform.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.hermes_llm import HermesSummarizerDeps
from lossless_hermes.summarize import LcmSummarizer
from lossless_hermes.synthesis.prompt_registry import (
    RegisterPromptOptions,
    register_prompt,
)
from lossless_hermes.synthesis.dispatch import LlmCallArgs, LlmCallResult
from lossless_hermes.tools._adapters import (
    _adapt_lcm_synthesize_around,
    _build_synthesizer_llm_call,
    _SynthesizeAroundCtx,
)
from lossless_hermes.tools.synthesize_around import SynthesizeAroundContext

# ---------------------------------------------------------------------------
# Skip marker — Apple system Python lacks enable_load_extension
# ---------------------------------------------------------------------------
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load and "
        "the engine hard-raises at construction. The on_session_start-"
        "fixtured / dispatch tests skip here; the bare-engine factory "
        "guard tests still run."
    ),
)


# ===========================================================================
# Test doubles — a HermesSummarizerDeps whose complete() is faked
# ===========================================================================


class _FakeCompleteDeps(HermesSummarizerDeps):
    """A :class:`HermesSummarizerDeps` with a deterministic fake ``complete``.

    The real ``complete`` lazy-imports Hermes's ``call_llm`` (unavailable
    in the test env). This subclass returns the cascade-required
    *block-list* envelope so :meth:`LcmSummarizer.summarize` round-trips.
    """

    SUMMARY_TEXT = "ADAPTER-FAKE-SUMMARY: condensed synthesis rollup."

    def complete(
        self,
        *,
        provider: str,
        model: str,
        api_key: str | None,
        system: str,
        user_prompt: str,
        max_tokens: int,
        reasoning: str | None = None,
        skip_model_auth: bool = False,
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        del (
            provider,
            model,
            api_key,
            system,
            user_prompt,
            max_tokens,
            reasoning,
            skip_model_auth,
            timeout_ms,
        )
        return {"content": [{"type": "text", "text": self.SUMMARY_TEXT}]}


class _FakeDepsEngine(LCMEngine):
    """An :class:`LCMEngine` whose ``on_session_start`` uses fake deps.

    Swaps the real :class:`HermesSummarizerDeps` for :class:`_FakeCompleteDeps`
    so ``engine.summarize`` / the ``build_llm_call`` factory run without a
    real LLM, while keeping every other line of the #164 PR-2 lifecycle
    wiring genuine. Mirrors the ``_ScriptedCompactEngine`` pattern in
    ``tests/tools/test_compact_adapter.py``.
    """

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        super().on_session_start(session_id, **kwargs)
        if self.deps is not None and not isinstance(self.deps, _FakeCompleteDeps):
            fake_deps = _FakeCompleteDeps()
            self.deps = fake_deps
            summarizer = LcmSummarizer(
                deps=fake_deps,
                config=self.config,
                provider_hint=self.config.summary_provider or None,
                model_hint=self.config.summary_model or None,
            )
            self._summarizer = summarizer
            self.summarize = summarizer.summarize


# ===========================================================================
# Fixtures + helpers
# ===========================================================================

#: An :class:`LcmConfig` with a configured summary model — so the
#: summarizer's candidate chain resolves a primary candidate and
#: ``build_llm_call`` can produce a model name.
_CONFIGURED = LcmConfig(summary_provider="test-provider", summary_model="test-model")


@pytest.fixture
def configured_engine(tmp_home: Path) -> Iterator[LCMEngine]:
    """A fake-deps :class:`LCMEngine` with ``on_session_start`` run + a summary model."""
    eng = _FakeDepsEngine(hermes_home=tmp_home / ".hermes", config=_CONFIGURED)
    eng.on_session_start("adapter-test-session")
    try:
        yield eng
    finally:
        eng.on_session_end("adapter-test-session", [])


def _bare_engine(*, config: LcmConfig | None = None) -> LCMEngine:
    """A bare :class:`LCMEngine` — no ``on_session_start``, no DB, no summarizer."""
    return LCMEngine(config=config if config is not None else LcmConfig())


def _seed_conversation_and_leaves(engine: LCMEngine, session_id: str) -> int:
    """Seed a conversation + two leaf summaries for ``session_id``.

    Returns the ``conversation_id``. The conversation is created with a
    non-``NULL`` ``session_key`` (== ``session_id`` — the engine is
    single-session-scoped), so the leaves' ``session_key`` (copied from
    the conversation row, ``NOT NULL`` on ``summaries``) is populated.
    The leaves fall inside the explicit since/before window the e2e
    test uses. Mirrors ``tests/tools/test_lcm_synthesize_around.py``'s
    ``_insert_leaf``.
    """
    store = engine._conversation_store
    assert store is not None
    # session_key == session_id — the conversation row carries a
    # non-NULL session_key so the leaf INSERT's subquery resolves.
    conv = store.get_or_create_conversation(session_id, session_key=session_id)
    db = engine._db
    assert db is not None
    for i, content in enumerate(("Leaf one content.", "Leaf two content."), start=1):
        db.execute(
            "INSERT INTO summaries"
            " (summary_id, conversation_id, kind, content, token_count,"
            "  session_key, created_at)"
            " VALUES (?, ?, 'leaf', ?, ?,"
            "         (SELECT session_key FROM conversations"
            "          WHERE conversation_id = ?), ?)",
            (
                f"sum_leaf_{i}",
                conv.conversation_id,
                content,
                max(1, (len(content) + 3) // 4),
                conv.conversation_id,
                f"2026-05-01 1{i}:00:00",
            ),
        )
    db.commit()
    return conv.conversation_id


# ===========================================================================
# _build_synthesizer_llm_call — the BuildLlmCall factory body
# ===========================================================================


@_skip_no_extension_loading
def test_build_synthesizer_llm_call_returns_callable_and_model(
    configured_engine: LCMEngine,
) -> None:
    """A configured engine → ``(LlmCall, model_name)``.

    The factory returns an awaitable :class:`~..synthesis.dispatch.LlmCall`
    and the resolved primary candidate's model. With ``summary_model="test-model"``
    the model name echoes the config — the Wave-12 F8 audit-honesty
    value the synthesis audit row records.
    """
    llm_call, model_name = _build_synthesizer_llm_call(configured_engine)
    assert callable(llm_call)
    assert model_name == "test-model"


@_skip_no_extension_loading
def test_build_synthesizer_llm_call_callable_is_awaitable_and_summarizes(
    configured_engine: LCMEngine,
) -> None:
    """Awaiting the factory's ``LlmCall`` yields the summary as an ``LlmCallResult``.

    The factory bridges the sync :meth:`LcmSummarizer.summarize` to the
    async :class:`~..synthesis.dispatch.LlmCall` Protocol. Awaiting it
    with an :class:`LlmCallArgs` runs the (fake-backed) summarizer and
    returns an :class:`LlmCallResult` whose ``output`` is the canned
    summary and whose ``actual_model`` is the resolved primary candidate.
    """
    llm_call, model_name = _build_synthesizer_llm_call(configured_engine)

    result = asyncio.run(
        llm_call(
            LlmCallArgs(
                model="ignored-dispatch-model",
                prompt="Synthesize these leaves into a rollup.",
                pass_kind="single",
                max_output_tokens=2_000,
            )
        )
    )
    assert isinstance(result, LlmCallResult)
    assert result.output == _FakeCompleteDeps.SUMMARY_TEXT
    # Wave-12 F8: actual_model is the resolved primary candidate, NOT the
    # dispatch-recommended model passed in LlmCallArgs.
    assert result.actual_model == model_name == "test-model"
    # Latency is measured (>= 0, a real float).
    assert isinstance(result.latency_ms, float)
    assert result.latency_ms >= 0.0


@_skip_no_extension_loading
def test_build_synthesizer_llm_call_raises_on_empty_candidate_chain(
    tmp_home: Path,
) -> None:
    """An engine with no ``summary_model`` → the factory raises ``RuntimeError``.

    A default :class:`LcmConfig` resolves an empty summarizer candidate
    chain. The factory raises ``RuntimeError`` *before* any cache row is
    written — the handler's ``try/except`` around ``ctx.build_llm_call()``
    converts that into the structured "No summarization model resolved"
    tool-error (see :func:`test_adapter_degrades_when_summarizer_unconfigured`).
    """
    eng = _FakeDepsEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("empty-chain-session")
    try:
        with pytest.raises(RuntimeError, match="no summary model candidates"):
            _build_synthesizer_llm_call(eng)
    finally:
        eng.on_session_end("empty-chain-session", [])


def test_build_synthesizer_llm_call_raises_on_bare_engine() -> None:
    """A bare engine (no ``on_session_start``) → the factory raises ``RuntimeError``.

    ``engine._summarizer`` is ``None`` until ``on_session_start``. The
    factory raises rather than dereferencing ``None`` — self-consistent
    even though the adapter's engine-readiness guard catches the unset
    state before the factory is ever built. Bare engine → runs on every
    platform.
    """
    eng = _bare_engine()
    assert eng._summarizer is None
    with pytest.raises(RuntimeError, match="summarizer is not initialised"):
        _build_synthesizer_llm_call(eng)


# ===========================================================================
# Structural conformance — _SynthesizeAroundCtx satisfies SynthesizeAroundContext
# ===========================================================================


@_skip_no_extension_loading
def test_synthesize_around_ctx_structurally_satisfies_protocol(
    configured_engine: LCMEngine,
) -> None:
    """``_SynthesizeAroundCtx`` is usable everywhere a :class:`SynthesizeAroundContext` is.

    :class:`SynthesizeAroundContext` is a runtime-uncheckable structural
    Protocol. ``ty`` enforces the match statically at the
    ``handle_lcm_synthesize_around(ctx=...)`` call site in ``_adapters.py``;
    this pins it at runtime too — the shim exposes ``conn`` /
    ``conversation_store`` / ``timezone`` plus a callable ``build_llm_call``.
    """
    assert configured_engine._db is not None
    assert configured_engine._conversation_store is not None

    def _factory() -> Any:
        return _build_synthesizer_llm_call(configured_engine)

    ctx = _SynthesizeAroundCtx(
        conn=configured_engine._db,
        conversation_store=configured_engine._conversation_store,
        timezone="UTC",
        build_llm_call=_factory,
    )
    bound: SynthesizeAroundContext = ctx  # static + runtime conformance
    assert isinstance(bound.conn, sqlite3.Connection)
    assert bound.conversation_store is configured_engine._conversation_store
    assert bound.timezone == "UTC"
    assert callable(bound.build_llm_call)


# ===========================================================================
# _adapt_lcm_synthesize_around — engine-state-unset graceful degrade
# ===========================================================================


def test_adapter_engine_db_unset_returns_structured_error() -> None:
    """A bare engine (no ``_db``) → a structured ``tool_result`` error, no raise.

    ``engine._db`` is ``None`` until ``on_session_start``. The adapter's
    engine-readiness guard degrades to a structured ``{"error": ...}``
    JSON string rather than raising an :class:`AttributeError`. Bare
    engine → runs on every platform.
    """
    engine = _bare_engine()
    assert engine._db is None
    result = _adapt_lcm_synthesize_around(
        {"window_kind": "period", "period": "yesterday"},
        ctx=engine,
    )
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert isinstance(parsed, dict)
    assert "error" in parsed
    assert "not" in parsed["error"] and "initialised" in parsed["error"], (
        f"expected the engine-not-ready structured error, got {result!r}"
    )
    # The error names the unset collaborator for the operator.
    assert "engine._db" in parsed["error"]


# ===========================================================================
# _adapt_lcm_synthesize_around — end-to-end dispatch through the adapter
# ===========================================================================


@_skip_no_extension_loading
def test_adapter_dispatches_end_to_end_and_writes_cache_row(
    configured_engine: LCMEngine,
) -> None:
    """The adapter dispatches synthesis end-to-end and writes a cache row.

    With a seeded conversation + leaves + a registered prompt, dispatching
    through :func:`_adapt_lcm_synthesize_around` runs the full path:
    ``SynthesizeAroundContext`` construction → window resolution → the
    ``build_llm_call`` factory → synthesis dispatch (against the fake
    ``complete``) → cache UPDATE. The result is a non-error
    ``tool_result`` and a ``lcm_synthesis_cache`` row in ``status='ready'``.

    This is the #164 plan PR-2 §4 "dispatch end-to-end ... assert a
    non-error tool_result and a lcm_synthesis_cache row" test — proof the
    summarizer surface (#164 PR-2 step 1) drives a real consumer.
    """
    session_id = "adapter-test-session"
    _seed_conversation_and_leaves(configured_engine, session_id)
    db = configured_engine._db
    assert db is not None
    register_prompt(
        db,
        RegisterPromptOptions(
            memory_type="episodic-condensed",
            tier_label="custom",
            pass_kind="single",
            template="Compact: {{source_text}}",
        ),
    )

    result = _adapt_lcm_synthesize_around(
        {
            "window_kind": "period",
            "since": "2026-05-01T00:00:00Z",
            "before": "2026-05-02T00:00:00Z",
        },
        ctx=configured_engine,
        # _dispatch_tool_call forwards the resolved session_key; the
        # engine is single-session-scoped, so session_key == session_id.
        session_key=session_id,
    )
    parsed = json.loads(result)
    assert isinstance(parsed, dict)
    assert "error" not in parsed, (
        f"adapter dispatch should succeed end-to-end, got error: {parsed.get('error')!r}"
    )
    # Success payload shape — markdown text + a cache_id.
    assert "text" in parsed
    assert _FakeCompleteDeps.SUMMARY_TEXT in parsed["text"]
    cache_id = parsed["cache_id"]
    assert isinstance(cache_id, str) and cache_id

    # A lcm_synthesis_cache row was written, in the ready state.
    cache_row = db.execute(
        "SELECT status, content, model_used FROM lcm_synthesis_cache WHERE cache_id = ?",
        (cache_id,),
    ).fetchone()
    assert cache_row is not None, "the synthesis cache row must be written"
    status, content, model_used = cache_row[0], cache_row[1], cache_row[2]
    assert status == "ready"
    assert content is not None and _FakeCompleteDeps.SUMMARY_TEXT in content
    # Wave-12 F8: the cache row records the resolved summarizer model.
    assert model_used == "test-model"


@_skip_no_extension_loading
def test_adapter_degrades_when_summarizer_unconfigured(
    tmp_home: Path,
) -> None:
    """No ``summary_model`` → the adapter degrades to a clean tool-error.

    With the default :class:`LcmConfig` the summarizer candidate chain is
    empty, so the ``build_llm_call`` factory raises ``RuntimeError``. The
    handler's ``try/except`` around ``ctx.build_llm_call()`` converts
    that into the structured "No summarization model resolved" tool-error
    — never an exception escape. This pins the
    factory-raises → handler-degrades seam end-to-end through the adapter.
    """
    eng = _FakeDepsEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("unconfigured-session")
    try:
        session_id = "unconfigured-session"
        _seed_conversation_and_leaves(eng, session_id)
        db = eng._db
        assert db is not None
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="custom",
                pass_kind="single",
                template="Compact: {{source_text}}",
            ),
        )

        result = _adapt_lcm_synthesize_around(
            {
                "window_kind": "period",
                "since": "2026-05-01T00:00:00Z",
                "before": "2026-05-02T00:00:00Z",
            },
            ctx=eng,
            session_key=session_id,
        )
        # The adapter did not raise — the handler caught the factory's
        # RuntimeError and returned a structured tool-error string.
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed
        assert "No summarization model resolved" in parsed["error"], (
            f"expected the structured 'No summarization model resolved' tool-error, got {result!r}"
        )
    finally:
        eng.on_session_end("unconfigured-session", [])
