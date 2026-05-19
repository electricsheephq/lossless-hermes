"""Tests for :mod:`lossless_hermes.hermes_llm` — the Hermes-backed
:class:`~lossless_hermes.summarize.SummarizerDeps` shim (issue #164 PR 1).

Test taxonomy
-------------

* **Envelope shape** — :meth:`HermesSummarizerDeps.complete` returns the
  block-list envelope ``{"content": [{"type": "text", "text": ...}]}``,
  and that envelope survives the real ``normalize_completion_summary``
  with a non-empty summary and is clean per ``extract_provider_auth_failure``
  / ``extract_provider_response_failure``.
* **call_llm wiring** — ``complete`` lazy-imports ``call_llm`` and calls
  it with the right ``task``, ``messages``, ``max_tokens``, ``timeout``.
* **api_key omission** — ``complete`` does NOT pass ``api_key`` when
  ``None``; passes it when truthy.
* **reasoning stripping** — inline ``<think>``/``<reasoning>`` blocks are
  stripped from the summary text.
* **error propagation** — a ``call_llm`` exception propagates out of
  ``complete`` unchanged.
* **deps surface** — ``get_api_key`` returns ``None`` (both
  ``skip_model_auth`` values); ``is_runtime_managed_auth_provider``
  returns ``True``.
* **Protocol conformance** — ``HermesSummarizerDeps`` structurally
  satisfies the ``SummarizerDeps`` Protocol.

No live network call is made: the ``call_llm`` symbol is replaced with a
recording fake via a synthetic ``agent.auxiliary_client`` module
installed into ``sys.modules`` (the shim lazy-imports it inside
``complete``, so the fake is picked up).

See:

* ``src/lossless_hermes/hermes_llm.py`` — module under test.
* ``epics/04-compaction`` / issue #164 — the compaction-P0 sequence.
* ``docs/porting-guides/assembler-compaction.md`` — summarizer guide.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Mapping
from typing import Any

import pytest

from lossless_hermes.hermes_llm import HermesSummarizerDeps
from lossless_hermes.summarize import (
    SummarizerDeps,
    extract_provider_auth_failure,
    extract_provider_response_failure,
    normalize_completion_summary,
)

# ---------------------------------------------------------------------------
# Fakes — an OpenAI-shape response + a recording ``call_llm``
# ---------------------------------------------------------------------------


class _FakeMessage:
    """Stand-in for ``response.choices[0].message``."""

    def __init__(self, content: Any) -> None:
        self.content = content


class _FakeChoice:
    """Stand-in for ``response.choices[0]``."""

    def __init__(self, content: Any) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    """Minimal OpenAI-shape chat-completion response."""

    def __init__(self, content: Any) -> None:
        self.choices = [_FakeChoice(content)]


class _RecordingCallLlm:
    """Recording fake for ``agent.auxiliary_client.call_llm``.

    Records every call's kwargs in :attr:`calls`. By default returns a
    canned response; ``content`` overrides the returned text/payload and
    ``raises`` makes the fake raise instead (the error-path test).
    """

    def __init__(
        self,
        *,
        content: Any = "A concise summary of the conversation.",
        raises: BaseException | None = None,
    ) -> None:
        self._content = content
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return _FakeResponse(self._content)


@pytest.fixture
def install_call_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Install a recording ``call_llm`` as ``agent.auxiliary_client``.

    Returns a factory: call it with the same kwargs as
    :class:`_RecordingCallLlm` to register the fake and get the recorder
    back. The synthetic ``agent`` / ``agent.auxiliary_client`` modules
    are installed into ``sys.modules`` so the shim's lazy
    ``from agent.auxiliary_client import call_llm`` resolves to the fake.
    ``monkeypatch`` restores ``sys.modules`` after the test.
    """

    def _factory(**kwargs: Any) -> _RecordingCallLlm:
        recorder = _RecordingCallLlm(**kwargs)
        agent_mod = types.ModuleType("agent")
        aux_mod = types.ModuleType("agent.auxiliary_client")
        aux_mod.call_llm = recorder  # type: ignore[attr-defined]
        agent_mod.auxiliary_client = aux_mod  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "agent", agent_mod)
        monkeypatch.setitem(sys.modules, "agent.auxiliary_client", aux_mod)
        return recorder

    return _factory


def _complete(deps: HermesSummarizerDeps, **overrides: Any) -> Mapping[str, Any]:
    """Call ``deps.complete`` with sane defaults, overridable per test."""
    kwargs: dict[str, Any] = {
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "api_key": None,
        "system": "You are a summarizer.",
        "user_prompt": "Summarize this conversation.",
        "max_tokens": 512,
        "timeout_ms": 30_000,
    }
    kwargs.update(overrides)
    return deps.complete(**kwargs)


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


class TestCompleteEnvelopeShape:
    """``complete()`` returns the mandatory block-list envelope."""

    def test_returns_block_list_envelope(self, install_call_llm: Any) -> None:
        """``content`` is a list of ``{"type": "text", "text": ...}`` blocks."""
        install_call_llm(content="The summary text.")
        result = _complete(HermesSummarizerDeps())

        assert isinstance(result, Mapping)
        content = result["content"]
        assert isinstance(content, list), "content MUST be a block list, not a string"
        assert content == [{"type": "text", "text": "The summary text."}]

    def test_envelope_has_no_top_level_status(self, install_call_llm: Any) -> None:
        """No top-level ``status`` int — it would read as an HTTP status."""
        install_call_llm(content="ok")
        result = _complete(HermesSummarizerDeps())
        assert "status" not in result
        assert "statusCode" not in result
        assert "status_code" not in result

    def test_envelope_finish_reason_is_benign(self, install_call_llm: Any) -> None:
        """``finish_reason`` is a benign value, not an error signal."""
        install_call_llm(content="ok")
        result = _complete(HermesSummarizerDeps())
        # Not in the {error, failed, cancelled} set extract_provider_response_failure flags.
        assert result.get("finish_reason") == "stop"

    def test_envelope_survives_normalize_completion_summary(self, install_call_llm: Any) -> None:
        """The envelope's ``content`` round-trips through the real cascade helper.

        This is the load-bearing assertion: ``normalize_completion_summary``
        is what the cascade actually feeds ``result["content"]`` into. A
        bare-string envelope would yield an empty summary here.
        """
        install_call_llm(content="A detailed multi-sentence summary of the thread.")
        result = _complete(HermesSummarizerDeps())

        summary, block_types = normalize_completion_summary(result["content"])
        assert summary == "A detailed multi-sentence summary of the thread."
        assert block_types == ["text"]

    def test_envelope_is_not_an_auth_failure(self, install_call_llm: Any) -> None:
        """A clean success envelope is NOT flagged by the auth detector.

        The cascade's success path calls ``extract_provider_auth_failure``
        with ``require_structural_signal=True``.
        """
        install_call_llm(content="ok")
        result = _complete(HermesSummarizerDeps())
        assert extract_provider_auth_failure(result, require_structural_signal=True) is None

    def test_envelope_is_not_a_response_failure(self, install_call_llm: Any) -> None:
        """A clean success envelope is NOT flagged by the response-failure detector."""
        install_call_llm(content="ok")
        result = _complete(HermesSummarizerDeps())
        assert extract_provider_response_failure(result) is None

    def test_none_content_yields_empty_text_block(self, install_call_llm: Any) -> None:
        """A ``None`` ``message.content`` coerces to an empty-text block.

        Still a well-formed block list — the cascade's empty-summary
        hardening (envelope -> retry -> fallback) handles emptiness.
        """
        install_call_llm(content=None)
        result = _complete(HermesSummarizerDeps())
        assert result["content"] == [{"type": "text", "text": ""}]

    def test_list_content_is_joined(self, install_call_llm: Any) -> None:
        """A list-shaped ``message.content`` is flattened to a string."""
        install_call_llm(content=[{"type": "text", "text": "part one "}, {"text": "part two"}])
        result = _complete(HermesSummarizerDeps())
        assert result["content"] == [{"type": "text", "text": "part one part two"}]


# ---------------------------------------------------------------------------
# call_llm wiring
# ---------------------------------------------------------------------------


class TestCompleteCallLlmWiring:
    """``complete()`` invokes ``call_llm`` with the correct arguments."""

    def test_lazy_imports_and_invokes_call_llm(self, install_call_llm: Any) -> None:
        """``complete`` resolves ``call_llm`` lazily and calls it exactly once."""
        recorder = install_call_llm(content="ok")
        _complete(HermesSummarizerDeps())
        assert len(recorder.calls) == 1

    def test_passes_lcm_summary_task(self, install_call_llm: Any) -> None:
        """``call_llm`` is called with ``task="lcm_summary"``."""
        recorder = install_call_llm(content="ok")
        _complete(HermesSummarizerDeps())
        assert recorder.calls[0]["task"] == "lcm_summary"

    def test_passes_system_and_user_messages(self, install_call_llm: Any) -> None:
        """``messages`` is a two-message system+user OpenAI-shape list."""
        recorder = install_call_llm(content="ok")
        _complete(
            HermesSummarizerDeps(),
            system="SYS PROMPT",
            user_prompt="USER PROMPT",
        )
        messages = recorder.calls[0]["messages"]
        assert messages == [
            {"role": "system", "content": "SYS PROMPT"},
            {"role": "user", "content": "USER PROMPT"},
        ]

    def test_passes_max_tokens(self, install_call_llm: Any) -> None:
        """``max_tokens`` is forwarded verbatim."""
        recorder = install_call_llm(content="ok")
        _complete(HermesSummarizerDeps(), max_tokens=777)
        assert recorder.calls[0]["max_tokens"] == 777

    def test_converts_timeout_ms_to_seconds(self, install_call_llm: Any) -> None:
        """``timeout_ms`` is converted to ``call_llm``'s seconds-based ``timeout``."""
        recorder = install_call_llm(content="ok")
        _complete(HermesSummarizerDeps(), timeout_ms=45_000)
        assert recorder.calls[0]["timeout"] == pytest.approx(45.0)

    def test_passes_low_temperature(self, install_call_llm: Any) -> None:
        """A low temperature is used for stable summaries."""
        recorder = install_call_llm(content="ok")
        _complete(HermesSummarizerDeps())
        assert recorder.calls[0]["temperature"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# api_key omission
# ---------------------------------------------------------------------------


class TestCompleteApiKeyHandling:
    """``complete()`` only forwards ``api_key`` when it is truthy."""

    def test_omits_api_key_when_none(self, install_call_llm: Any) -> None:
        """``api_key=None`` is NOT passed to ``call_llm`` (lets it auto-resolve)."""
        recorder = install_call_llm(content="ok")
        _complete(HermesSummarizerDeps(), api_key=None)
        assert "api_key" not in recorder.calls[0]

    def test_omits_api_key_when_empty_string(self, install_call_llm: Any) -> None:
        """An empty-string ``api_key`` is also treated as absent."""
        recorder = install_call_llm(content="ok")
        _complete(HermesSummarizerDeps(), api_key="")
        assert "api_key" not in recorder.calls[0]

    def test_forwards_api_key_when_truthy(self, install_call_llm: Any) -> None:
        """A non-empty ``api_key`` IS forwarded to ``call_llm``."""
        recorder = install_call_llm(content="ok")
        _complete(HermesSummarizerDeps(), api_key="sk-secret")
        assert recorder.calls[0].get("api_key") == "sk-secret"


# ---------------------------------------------------------------------------
# Reasoning-block stripping
# ---------------------------------------------------------------------------


class TestCompleteReasoningStripping:
    """``complete()`` strips inline reasoning blocks from the summary."""

    def test_strips_think_block(self, install_call_llm: Any) -> None:
        """An inline ``<think>...</think>`` block is removed."""
        install_call_llm(content="<think>let me consider</think>The actual summary.")
        result = _complete(HermesSummarizerDeps())
        assert result["content"] == [{"type": "text", "text": "The actual summary."}]

    def test_strips_reasoning_block(self, install_call_llm: Any) -> None:
        """An inline ``<reasoning>...</reasoning>`` block is removed."""
        install_call_llm(content="<reasoning>chain of thought</reasoning>Clean summary.")
        result = _complete(HermesSummarizerDeps())
        assert result["content"] == [{"type": "text", "text": "Clean summary."}]

    def test_strips_multiline_reasoning_block(self, install_call_llm: Any) -> None:
        """A multi-line reasoning block is removed (DOTALL)."""
        install_call_llm(content="<thinking>line one\nline two\nline three</thinking>Summary body.")
        result = _complete(HermesSummarizerDeps())
        assert result["content"] == [{"type": "text", "text": "Summary body."}]

    def test_leaves_clean_text_untouched(self, install_call_llm: Any) -> None:
        """Text with no reasoning tags is returned unchanged (idempotent)."""
        install_call_llm(content="Just a plain summary, no tags.")
        result = _complete(HermesSummarizerDeps())
        assert result["content"] == [{"type": "text", "text": "Just a plain summary, no tags."}]


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestCompleteErrorPropagation:
    """``complete()`` lets ``call_llm`` exceptions propagate unchanged."""

    def test_propagates_runtime_error(self, install_call_llm: Any) -> None:
        """A ``RuntimeError`` from ``call_llm`` bubbles out of ``complete``.

        The cascade's caught-error path (``_attempt_summarizer_call``)
        synthesizes a diagnostic view from the exception — richer than a
        hand-rolled error envelope — so propagation is correct.
        """
        boom = RuntimeError("No LLM provider configured for task=lcm_summary")
        install_call_llm(raises=boom)
        with pytest.raises(RuntimeError, match="No LLM provider configured"):
            _complete(HermesSummarizerDeps())

    def test_propagates_auth_shaped_exception(self, install_call_llm: Any) -> None:
        """A 401-shaped exception propagates so the cascade can coerce it.

        ``summarize.py``'s ``_coerce_to_dict`` reads ``status`` /
        ``response`` attrs off the exception; the caught-error path then
        detects the auth failure. ``complete()`` must NOT swallow it.
        """

        class _FakeAuthError(Exception):
            def __init__(self) -> None:
                super().__init__("Unauthorized")
                self.status = 401

        install_call_llm(raises=_FakeAuthError())
        with pytest.raises(_FakeAuthError) as exc_info:
            _complete(HermesSummarizerDeps())
        # The exception still carries the structural 401 the cascade reads.
        assert exc_info.value.status == 401
        auth_failure = extract_provider_auth_failure(exc_info.value)
        assert auth_failure is not None
        assert auth_failure.status_code == 401


# ---------------------------------------------------------------------------
# get_api_key / is_runtime_managed_auth_provider
# ---------------------------------------------------------------------------


class TestDepsSurface:
    """``get_api_key`` / ``is_runtime_managed_auth_provider`` behavior."""

    def test_get_api_key_returns_none(self) -> None:
        """``get_api_key`` returns ``None`` — auth is deferred to ``call_llm``."""
        deps = HermesSummarizerDeps()
        assert deps.get_api_key("anthropic", "claude-haiku-4-5") is None

    def test_get_api_key_returns_none_with_skip_model_auth(self) -> None:
        """``get_api_key`` returns ``None`` on the ``skip_model_auth`` path too.

        The cascade reads a ``None`` direct key as "auth retry
        unavailable" and cleanly raises the original auth error.
        """
        deps = HermesSummarizerDeps()
        assert deps.get_api_key("anthropic", "claude-haiku-4-5", skip_model_auth=True) is None
        assert deps.get_api_key("openai", "gpt-4o-mini", skip_model_auth=False) is None

    def test_is_runtime_managed_auth_provider_returns_true(self) -> None:
        """``is_runtime_managed_auth_provider`` returns ``True`` for any provider.

        This short-circuits the cascade's ``skip_model_auth`` retry path,
        which would need a credential the shim does not provide.
        """
        deps = HermesSummarizerDeps()
        assert deps.is_runtime_managed_auth_provider("anthropic") is True
        assert deps.is_runtime_managed_auth_provider("openai") is True
        assert deps.is_runtime_managed_auth_provider("custom") is True


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def _typecheck(_deps: SummarizerDeps) -> None:
    """No-op consumer used to assert structural Protocol conformance.

    Passing a ``HermesSummarizerDeps`` here only type-checks (``ty``) if
    the class genuinely satisfies every ``SummarizerDeps`` member with a
    compatible signature — the same pattern ``tools/_adapters.py`` uses
    for adapter <-> Protocol checks.
    """


class TestProtocolConformance:
    """``HermesSummarizerDeps`` satisfies the ``SummarizerDeps`` Protocol."""

    def test_satisfies_summarizer_deps_protocol(self) -> None:
        """A ``HermesSummarizerDeps`` instance is accepted as a ``SummarizerDeps``."""
        _typecheck(HermesSummarizerDeps())

    def test_is_usable_as_lcm_summarizer_deps(self, install_call_llm: Any) -> None:
        """End-to-end: the shim drives ``LcmSummarizer`` with no real network call.

        Constructs a real ``LcmSummarizer`` with the shim as its ``deps``
        and a recording ``call_llm``; ``summarize`` returns the canned
        summary string, proving the shim's envelope is consumed correctly
        by the cascade's normalization path.
        """
        from lossless_hermes.db.config import LcmConfig
        from lossless_hermes.summarize import LcmSummarizer

        install_call_llm(content="Cascade-consumed summary string.")
        summarizer = LcmSummarizer(
            deps=HermesSummarizerDeps(),
            config=LcmConfig(),
            provider_hint="anthropic",
            model_hint="claude-haiku-4-5",
            # Empty env so ambient LCM_SUMMARY_* vars cannot shift the
            # candidate chain — the layer-5 hint resolves the candidate.
            env={},
            # No-op backoff so the test never sleeps.
            sleep=lambda _seconds: None,
        )
        # A long-enough input so the summary is accepted as a reduction.
        source = "The conversation covered many topics. " * 40
        result = summarizer.summarize(source)
        assert result == "Cascade-consumed summary string."
