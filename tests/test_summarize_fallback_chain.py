"""Tests for :mod:`lossless_hermes.summarize` fallback cascade (issue 04-06).

Covers the cascade landed in this issue (the 5-layer candidate
resolution, the per-candidate auth/timeout retry loop, the
exponential-backoff scheduling, and the deterministic-fallback exit).
The prompt-builder snapshot tests live in ``test_summarize_prompts.py``
(issue 04-05).

### Test taxonomy

* **Auth detection** — :func:`extract_provider_auth_failure` and
  :func:`extract_provider_response_failure` invariants. Mirror the
  TS unit tests in ``test/summarize.test.ts:756-995``.
* **Candidate resolution** — :func:`resolve_summary_candidates`
  layer-precedence and dedup behavior. Mirror
  ``test/summarize.test.ts:95-380``.
* **Normalization** — :func:`normalize_completion_summary` drops
  reasoning blocks, dedupes first-seen, recovers via envelope.
  Mirror ``test/summarize.test.ts:632-755``.
* **Target tokens** — :func:`resolve_target_tokens` floors and
  ratios. Mirror ``test/summarize.test.ts:417-460``.
* **Cascade** — :meth:`LcmSummarizer.summarize` falls through on
  auth, times out cleanly, raises on all-candidate auth fail,
  applies the backoff schedule. Mirror
  ``test/summarize.test.ts:546-1605``.
* **Timeout** — :func:`_with_timeout` uses ThreadPoolExecutor per
  ADR-017 (smoke-grep test in addition to behavioral test).

See:

* ``epics/04-compaction/04-06-summarize-fallback-chain.md`` — AC.
* ``docs/adr/017-async-policy.md`` — ThreadPoolExecutor pattern.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment policy.
* ``lossless-claw/src/summarize.ts:1131-1696`` — TS source.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import pytest

from lossless_hermes.db.config import FallbackProvider, LcmConfig
from lossless_hermes.summarize import (
    LcmProviderAuthError,
    LcmSummarizer,
    ProviderAuthFailure,
    ResolvedSummaryCandidate,
    SummarizerTimeoutError,
    _compute_backoff_ms,
    _with_timeout,
    extract_incomplete_response_signals,
    extract_provider_auth_failure,
    extract_provider_response_failure,
    normalize_completion_summary,
    resolve_summary_candidates,
    resolve_target_tokens,
)


# ---------------------------------------------------------------------------
# Helpers — a minimal SummarizerDeps fake
# ---------------------------------------------------------------------------


class _FakeDeps:
    """Recording deps fake — captures calls + returns scripted results.

    The ``script`` list is consumed in FIFO order. Each entry is either:

    * A ``Mapping`` → returned as the completion result.
    * An ``Exception`` instance → raised.
    * A callable → invoked with the call kwargs, return value used.

    The fake also tracks ``get_api_key`` calls, the order of
    ``complete`` calls (provider/model), and exposes
    ``is_runtime_managed`` for the ``skip_model_auth`` gate test.
    """

    def __init__(
        self,
        script: list[Any] | None = None,
        *,
        api_keys: dict[tuple[str, str, bool], str | None] | None = None,
        runtime_managed_providers: set[str] | None = None,
    ) -> None:
        self.script = list(script or [])
        self._api_keys = api_keys or {}
        self._runtime_managed = runtime_managed_providers or set()
        self.complete_calls: list[dict[str, Any]] = []
        self.get_api_key_calls: list[tuple[str, str, bool]] = []

    def complete(self, **kwargs: Any) -> Mapping[str, Any]:
        self.complete_calls.append(kwargs)
        if not self.script:
            raise AssertionError(
                "FakeDeps.complete called with no script entries left; "
                f"complete_calls so far={len(self.complete_calls)}"
            )
        entry = self.script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            return entry(kwargs)
        return entry

    def get_api_key(
        self, provider: str, model: str, *, skip_model_auth: bool = False
    ) -> str | None:
        self.get_api_key_calls.append((provider, model, skip_model_auth))
        key = self._api_keys.get((provider, model, skip_model_auth))
        if key is None and not skip_model_auth:
            return "fake-api-key"
        return key

    def is_runtime_managed_auth_provider(self, provider: str) -> bool:
        return provider in self._runtime_managed


def _content(text: str) -> dict[str, Any]:
    """Build a minimal Anthropic-style content envelope."""
    return {"content": [{"type": "text", "text": text}]}


# =============================================================================
# Auth detection — extract_provider_auth_failure
# =============================================================================


class TestExtractProviderAuthFailure:
    """:func:`extract_provider_auth_failure` invariants."""

    def test_structural_signal_required_triggers_on_401(self) -> None:
        """``require_structural_signal=True`` → HTTP 401 IS a structural signal."""
        result = extract_provider_auth_failure(
            {"status": 401, "message": "unauthorized"},
            require_structural_signal=True,
        )
        assert result is not None
        assert result.status_code == 401

    def test_structural_signal_required_triggers_on_explicit_error_kind(self) -> None:
        """``error.kind="provider_auth"`` is the other structural signal."""
        result = extract_provider_auth_failure(
            {"error": {"kind": "provider_auth", "message": "missing scope"}},
            require_structural_signal=True,
        )
        assert result is not None

    def test_structural_signal_required_skips_plain_text_match(self) -> None:
        """Plain "unauthorized" text in a 200 response → NOT a failure.

        Wave-N-adjacent invariant: the LLM summary may legitimately
        discuss auth errors. Only structural signals count when checking
        a success-path envelope.
        """
        result = extract_provider_auth_failure(
            {
                "status": 200,
                "content": [{"type": "text", "text": "The user mentioned unauthorized access"}],
            },
            require_structural_signal=True,
        )
        assert result is None

    def test_non_structural_triggers_on_text_pattern(self) -> None:
        """``require_structural_signal=False`` matches the broad text pattern."""
        result = extract_provider_auth_failure(
            {"error": "invalid api key"}, require_structural_signal=False
        )
        assert result is not None

    def test_non_structural_triggers_on_model_request_scope(self) -> None:
        """``model.request`` scope signal → missing_model_request_scope=True."""
        result = extract_provider_auth_failure(
            {"errorMessage": "missing scope model.request"},
            require_structural_signal=False,
        )
        assert result is not None
        assert result.missing_model_request_scope is True

    def test_non_structural_does_not_trigger_on_random_text(self) -> None:
        """No auth markers → returns None even on non-structural path."""
        result = extract_provider_auth_failure(
            {"errorMessage": "rate limit"}, require_structural_signal=False
        )
        assert result is None

    def test_exception_with_status_attribute_detected(self) -> None:
        """HTTP errors carrying ``status`` / ``status_code`` attrs detected."""

        class FakeHttpError(Exception):
            status_code = 401

        result = extract_provider_auth_failure(
            FakeHttpError("401 Unauthorized"), require_structural_signal=False
        )
        assert result is not None
        assert result.status_code == 401

    def test_nested_envelope_status_code_detected(self) -> None:
        """Status code nested under ``response.status`` is found."""
        result = extract_provider_auth_failure(
            {"response": {"status": 401}}, require_structural_signal=True
        )
        assert result is not None
        assert result.status_code == 401


# =============================================================================
# Response failure — extract_provider_response_failure
# =============================================================================


class TestExtractProviderResponseFailure:
    """:func:`extract_provider_response_failure` invariants."""

    def test_triggers_on_finish_error(self) -> None:
        """``finish_reason=error`` is a structural failure signal."""
        result = extract_provider_response_failure({
            "finish_reason": "error",
            "message": "bad request",
        })
        assert result is not None
        assert result.finish_reason == "error"

    def test_triggers_on_4xx_status(self) -> None:
        """HTTP ≥ 400 is a structural failure signal."""
        result = extract_provider_response_failure({"status": 503, "message": "down"})
        assert result is not None
        assert result.status_code == 503

    def test_skips_provider_auth_error_kind(self) -> None:
        """``error.kind=provider_auth`` is owned by the auth detector, not this one."""
        result = extract_provider_response_failure({"error": {"kind": "provider_auth"}})
        assert result is None

    def test_triggers_on_non_auth_error_kind(self) -> None:
        """Other ``error.kind`` values fire this detector."""
        result = extract_provider_response_failure({"error": {"kind": "rate_limit"}})
        assert result is not None

    def test_clean_response_returns_none(self) -> None:
        """200 response with content → not a failure."""
        result = extract_provider_response_failure({
            "status": 200,
            "content": [{"type": "text", "text": "ok"}],
        })
        assert result is None


# =============================================================================
# Normalization — normalize_completion_summary
# =============================================================================


class TestNormalizeCompletionSummary:
    """:func:`normalize_completion_summary` invariants."""

    def test_drops_reasoning_blocks(self) -> None:
        """Blocks with ``type`` containing "reasoning" are dropped."""
        content = [
            {"type": "reasoning", "text": "secret chain of thought"},
            {"type": "text", "text": "the summary"},
        ]
        summary, types = normalize_completion_summary(content)
        assert "secret chain of thought" not in summary
        assert "the summary" in summary
        assert "reasoning" in types

    def test_drops_thinking_blocks(self) -> None:
        """Blocks with ``type`` containing "thinking" are dropped (Anthropic-style)."""
        content = [
            {"type": "thinking", "text": "internal monologue"},
            {"type": "text", "text": "actual content"},
        ]
        summary, _ = normalize_completion_summary(content)
        assert "internal monologue" not in summary
        assert "actual content" in summary

    def test_dedupe_first_seen_wins(self) -> None:
        """Exact duplicate fragments are deduped preserving first-seen order."""
        content = [
            {"type": "text", "text": "alpha"},
            {"type": "text", "text": "beta"},
            {"type": "text", "text": "alpha"},
        ]
        summary, _ = normalize_completion_summary(content)
        # First-seen order: alpha THEN beta. Without dedup the output
        # would be "alpha\nbeta\nalpha". With dedup it's "alpha\nbeta".
        assert summary == "alpha\nbeta"

    def test_envelope_aware_extraction(self) -> None:
        """Passing the full envelope (not just ``content``) recovers text from ``output_text``."""
        envelope = {"content": [], "output_text": "envelope-rescued summary"}
        summary, _ = normalize_completion_summary(envelope)
        assert "envelope-rescued summary" in summary

    def test_empty_content_returns_empty_string(self) -> None:
        """No text-like fields → empty string."""
        summary, types = normalize_completion_summary([])
        assert summary == ""
        assert types == []


# =============================================================================
# Incomplete signals
# =============================================================================


def test_incomplete_response_signals_detected() -> None:
    """``incomplete_details.reason`` and ``status="incomplete"`` are collected."""
    envelope = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_tokens"},
        "content": [{"type": "text", "text": "partial output"}],
    }
    signals = extract_incomplete_response_signals(envelope)
    assert any("status=incomplete" in s for s in signals)
    assert any("max_tokens" in s for s in signals)


# =============================================================================
# Target token resolution
# =============================================================================


class TestResolveTargetTokens:
    """:func:`resolve_target_tokens` per TS lines 855-873."""

    def test_condensed_min_floor_512(self) -> None:
        """Condensed → max(512, condensed_target_tokens). 200 → 512 floor."""
        assert (
            resolve_target_tokens(
                input_tokens=5000,
                mode="normal",
                is_condensed=True,
                leaf_target_tokens=4000,
                condensed_target_tokens=200,
            )
            == 512
        )

    def test_condensed_above_floor(self) -> None:
        """Condensed config value above floor wins."""
        assert (
            resolve_target_tokens(
                input_tokens=5000,
                mode="normal",
                is_condensed=True,
                leaf_target_tokens=4000,
                condensed_target_tokens=2000,
            )
            == 2000
        )

    def test_leaf_normal_floor_192(self) -> None:
        """Leaf normal with tiny input → 192 floor."""
        # 1 token * 0.35 = 0, capped at 192.
        assert (
            resolve_target_tokens(
                input_tokens=1,
                mode="normal",
                is_condensed=False,
                leaf_target_tokens=4000,
            )
            == 192
        )

    def test_leaf_aggressive_floor_96(self) -> None:
        """Leaf aggressive with tiny input → 96 floor."""
        # 1 token * 0.20 = 0, capped at 96.
        assert (
            resolve_target_tokens(
                input_tokens=1,
                mode="aggressive",
                is_condensed=False,
                leaf_target_tokens=4000,
            )
            == 96
        )

    def test_leaf_normal_035_ratio(self) -> None:
        """Leaf normal at moderate input → input * 0.35 floored."""
        # 1000 tokens * 0.35 = 350, well below 4000 cap and above 192 floor.
        assert (
            resolve_target_tokens(
                input_tokens=1000,
                mode="normal",
                is_condensed=False,
                leaf_target_tokens=4000,
            )
            == 350
        )

    def test_leaf_aggressive_cap_below_normal(self) -> None:
        """Aggressive cap = floor(leaf * 0.55) = 2200 for leaf=4000."""
        # 100000 tokens * 0.20 would be 20000, but aggressive_cap caps at 2200.
        assert (
            resolve_target_tokens(
                input_tokens=100000,
                mode="aggressive",
                is_condensed=False,
                leaf_target_tokens=4000,
            )
            == 2200
        )


# =============================================================================
# Backoff
# =============================================================================


class TestComputeBackoffMs:
    """``min(500 * 2^index, 8000)`` per TS lines 1498-1525."""

    def test_first_candidate(self) -> None:
        assert _compute_backoff_ms(0) == 500

    def test_second_candidate(self) -> None:
        assert _compute_backoff_ms(1) == 1000

    def test_third_candidate(self) -> None:
        assert _compute_backoff_ms(2) == 2000

    def test_fourth_candidate(self) -> None:
        assert _compute_backoff_ms(3) == 4000

    def test_fifth_candidate(self) -> None:
        assert _compute_backoff_ms(4) == 8000

    def test_sixth_and_beyond_capped(self) -> None:
        """The 6th candidate uses 8000 (not 16000) — invariant from the spec."""
        assert _compute_backoff_ms(5) == 8000
        assert _compute_backoff_ms(10) == 8000


# =============================================================================
# Timeout
# =============================================================================


class TestWithTimeout:
    """:func:`_with_timeout` uses ThreadPoolExecutor per ADR-017."""

    def test_returns_result_on_success(self) -> None:
        result = _with_timeout(lambda: 42, timeout_ms=1000, label="test")
        assert result == 42

    def test_raises_summarizer_timeout(self) -> None:
        """Slow callable → SummarizerTimeoutError, not a generic TimeoutError."""

        def slow_call() -> str:
            time.sleep(2.0)
            return "never"

        with pytest.raises(SummarizerTimeoutError) as exc:
            _with_timeout(slow_call, timeout_ms=100, label="slow-test")
        assert "slow-test" in str(exc.value)
        assert "100ms" in str(exc.value)

    def test_module_uses_thread_pool_executor_not_asyncio(self) -> None:
        """ADR-017 smoke test — the module imports ThreadPoolExecutor, not asyncio.wait_for.

        This catches a refactor that "modernizes" the timeout via asyncio
        and accidentally forces the whole call chain async. Strips
        docstring / comment lines first so the negative reference in
        ADR-017 commentary doesn't trip the matcher.
        """
        import pathlib
        import re

        import lossless_hermes.summarize as mod

        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        assert "ThreadPoolExecutor" in src, (
            "ADR-017 violation: summarize.py must use ThreadPoolExecutor for "
            "the sync timeout pattern, not asyncio.wait_for."
        )
        # Strip docstrings + single-line comments before checking for the
        # negative `asyncio.wait_for` reference — the rationale strings in
        # the module body deliberately MENTION asyncio.wait_for as the
        # rejected alternative.
        no_docstrings = re.sub(r'"""[\s\S]*?"""', "", src)
        no_comments = re.sub(r"#.*", "", no_docstrings)
        assert "asyncio.wait_for" not in no_comments, (
            "ADR-017 violation: summarize.py must NOT use asyncio.wait_for in "
            "executable code — the sync call chain matches Hermes's "
            "auxiliary_client.call_llm contract."
        )
        # Sanity: also ensure no plain `import asyncio` appears in
        # executable code.
        assert "\nimport asyncio" not in no_comments
        assert "\nfrom asyncio" not in no_comments


# =============================================================================
# Candidate resolution
# =============================================================================


class TestResolveSummaryCandidates:
    """5-layer resolution per TS lines 1131-1250."""

    def test_layer_1_env_vars_highest_precedence(self) -> None:
        """LCM_SUMMARY_MODEL + LCM_SUMMARY_PROVIDER win over plugin config."""
        config = LcmConfig(summary_provider="plugin-prov", summary_model="plugin-model")
        env = {"LCM_SUMMARY_PROVIDER": "env-prov", "LCM_SUMMARY_MODEL": "env-model"}
        candidates = resolve_summary_candidates(config=config, env=env)
        assert candidates[0].provider == "env-prov"
        assert candidates[0].model == "env-model"
        assert candidates[0].level_name == "environment variables"

    def test_layer_2_plugin_config_when_env_absent(self) -> None:
        """Plugin config is layer 2."""
        config = LcmConfig(summary_provider="plugin-prov", summary_model="plugin-model")
        candidates = resolve_summary_candidates(config=config, env={})
        assert candidates[0].provider == "plugin-prov"
        assert candidates[0].model == "plugin-model"

    def test_layer_5_legacy_runtime_hint(self) -> None:
        """When no config layer matches, the legacy provider/model hint wins."""
        config = LcmConfig()  # All summary_* fields empty
        candidates = resolve_summary_candidates(
            config=config,
            provider_hint="anthropic",
            model_hint="claude-3-7-sonnet",
            env={},
        )
        assert len(candidates) >= 1
        assert candidates[0].provider == "anthropic"
        assert candidates[0].model == "claude-3-7-sonnet"
        assert candidates[0].use_legacy_auth_profile is True

    def test_dedup_on_provider_model_tuple(self) -> None:
        """Two layers resolving to the same ``(provider, model)`` → ONE entry."""
        config = LcmConfig(summary_provider="anthropic", summary_model="claude-3-7-sonnet")
        candidates = resolve_summary_candidates(
            config=config,
            provider_hint="anthropic",
            model_hint="claude-3-7-sonnet",
            env={
                "LCM_SUMMARY_PROVIDER": "anthropic",
                "LCM_SUMMARY_MODEL": "claude-3-7-sonnet",
            },
        )
        # Three layers all point at the same pair → must dedup to 1.
        assert len(candidates) == 1

    def test_explicit_fallback_providers_appended(self) -> None:
        """``fallback_providers`` are appended to the chain (NOT replacing)."""
        config = LcmConfig(
            summary_provider="anthropic",
            summary_model="claude-3-7-sonnet",
            fallback_providers=[
                FallbackProvider(provider="openai", model="gpt-4o"),
            ],
        )
        candidates = resolve_summary_candidates(config=config, env={})
        # Primary layer plus explicit fallback.
        assert len(candidates) == 2
        assert candidates[0].provider == "anthropic"
        assert candidates[-1].provider == "openai"
        assert "explicit fallback" in candidates[-1].level_name

    def test_slash_notation_parses(self) -> None:
        """``provider/model`` slash notation parses without an explicit provider."""
        config = LcmConfig(summary_model="anthropic/claude-3-7-sonnet")
        candidates = resolve_summary_candidates(config=config, env={})
        assert len(candidates) == 1
        assert candidates[0].provider == "anthropic"
        assert candidates[0].model == "claude-3-7-sonnet"


# =============================================================================
# Cascade — LcmSummarizer end-to-end
# =============================================================================


def _make_summarizer(
    deps: _FakeDeps,
    *,
    candidates: list[tuple[str, str]] | None = None,
    config: LcmConfig | None = None,
    sleep: Any = None,
) -> LcmSummarizer:
    """Construct LcmSummarizer with an explicit candidate list (test convenience)."""
    if config is None:
        config = LcmConfig()
    # Bypass normal resolution by patching the candidates list directly.
    summarizer = LcmSummarizer(
        deps=deps,
        config=config,
        provider_hint="any",
        model_hint="any-model",
        env={},
        sleep=sleep or (lambda _s: None),
    )
    if candidates is not None:
        summarizer.candidates = [
            ResolvedSummaryCandidate(level_name=f"test layer {i}", provider=p, model=m)
            for i, (p, m) in enumerate(candidates)
        ]
    return summarizer


class TestLcmSummarizerCascade:
    """The 5-layer fallback chain behavior per TS lines 1295-1696."""

    def test_empty_text_short_circuits(self) -> None:
        """Empty/whitespace-only input → ``""`` without any LLM call."""
        deps = _FakeDeps([])
        s = _make_summarizer(deps, candidates=[("anthropic", "claude-3-7-sonnet")])
        assert s.summarize("") == ""
        assert s.summarize("   \n\t  ") == ""
        assert deps.complete_calls == []

    def test_empty_candidates_raises(self) -> None:
        """No candidates resolved → RuntimeError."""
        deps = _FakeDeps([_content("ignored")])
        s = LcmSummarizer(
            deps=deps,
            config=LcmConfig(),
            provider_hint=None,
            model_hint=None,
            env={},
            sleep=lambda _s: None,
        )
        # No env, no plugin config, no hints → empty.
        assert s.candidates == []
        with pytest.raises(RuntimeError):
            s.summarize("hello world")

    def test_first_candidate_success(self) -> None:
        """First candidate returns content → cascade exits with that summary."""
        deps = _FakeDeps([_content("a clean summary")])
        s = _make_summarizer(deps, candidates=[("anthropic", "claude-3-7-sonnet")])
        result = s.summarize("hello world")
        assert result == "a clean summary"
        assert len(deps.complete_calls) == 1
        assert deps.complete_calls[0]["provider"] == "anthropic"

    def test_auth_failure_falls_to_next_candidate(self) -> None:
        """First candidate auth-fails → second candidate is tried."""
        deps = _FakeDeps(
            [
                {"status": 401, "message": "unauthorized"},  # initial fails
                # ``get_api_key(skip_model_auth=True)`` returns None → reraises auth
                {"content": [{"type": "text", "text": "second-candidate summary"}]},
            ],
            api_keys={
                ("anthropic", "claude-3-7-sonnet", True): None,  # no direct creds
            },
        )
        s = _make_summarizer(
            deps,
            candidates=[
                ("anthropic", "claude-3-7-sonnet"),
                ("openai", "gpt-4o"),
            ],
        )
        result = s.summarize("hello world")
        assert result == "second-candidate summary"
        # Two complete calls: one to the auth-failing primary, one to the fallback.
        # (The skip-model-auth retry is skipped because get_api_key returns None.)
        assert len(deps.complete_calls) == 2
        assert deps.complete_calls[0]["provider"] == "anthropic"
        assert deps.complete_calls[1]["provider"] == "openai"

    def test_all_auth_fail_raises_lcm_provider_auth_error(self) -> None:
        """ALL candidates auth-fail → LcmProviderAuthError RAISED.

        Auth-short-circuit invariant (Wave-N): the caller catches this
        and skips persistence to preserve DAG integrity. The cascade
        does NOT return a deterministic fallback in this case.
        """
        deps = _FakeDeps(
            [
                {"status": 401, "message": "unauthorized"},
                {"status": 401, "message": "unauthorized again"},
            ],
            api_keys={
                ("anthropic", "claude-3-7-sonnet", True): None,
                ("openai", "gpt-4o", True): None,
            },
        )
        s = _make_summarizer(
            deps,
            candidates=[
                ("anthropic", "claude-3-7-sonnet"),
                ("openai", "gpt-4o"),
            ],
        )
        with pytest.raises(LcmProviderAuthError):
            s.summarize("hello world")

    def test_all_timeout_returns_deterministic_fallback(self) -> None:
        """ALL candidates time out → deterministic fallback (NOT raise).

        Wave-4 P0 invariant: the fallback ALWAYS carries the
        ``[LCM fallback summary — model unavailable; ...]`` marker.
        """
        deps = _FakeDeps(
            [
                SummarizerTimeoutError(100, "initial"),
                SummarizerTimeoutError(100, "initial"),
            ],
        )
        s = _make_summarizer(
            deps,
            candidates=[
                ("anthropic", "claude-3-7-sonnet"),
                ("openai", "gpt-4o"),
            ],
        )
        result = s.summarize("hello world")
        # Wave-4 invariant: marker is present.
        assert "[LCM fallback summary" in result
        assert "hello world" in result

    def test_skip_model_auth_retry_succeeds(self) -> None:
        """Auth failure → skip_model_auth retry succeeds → return that summary."""
        deps = _FakeDeps(
            [
                # Initial: throws an auth error from .complete.
                Exception("HTTP 401 unauthorized"),
                # Auth retry: returns a clean summary.
                _content("retried-with-direct-creds"),
            ],
            api_keys={
                ("anthropic", "claude-3-7-sonnet", True): "direct-key",
            },
        )
        s = _make_summarizer(deps, candidates=[("anthropic", "claude-3-7-sonnet")])
        result = s.summarize("hello world")
        assert result == "retried-with-direct-creds"
        # Two complete calls: initial + auth-retry.
        assert len(deps.complete_calls) == 2
        # Second call should have skip_model_auth=True.
        assert deps.complete_calls[1]["skip_model_auth"] is True

    def test_skip_model_auth_retry_skipped_for_runtime_managed(self) -> None:
        """Runtime-managed providers SKIP the skip_model_auth retry.

        OAuth-managed providers cannot use direct credentials, and
        attempting the bypass would surface a misleading
        "no credentials found" error to the operator.
        """
        deps = _FakeDeps(
            [
                # Auth-failing structural envelope on candidate 1.
                {"status": 401, "message": "unauthorized"},
                # Candidate 2 succeeds.
                _content("candidate-2-summary"),
            ],
            runtime_managed_providers={"openclaw-runtime"},
        )
        s = _make_summarizer(
            deps,
            candidates=[
                ("openclaw-runtime", "managed-model"),
                ("anthropic", "claude-3-7-sonnet"),
            ],
        )
        result = s.summarize("hello world")
        assert result == "candidate-2-summary"
        # Only TWO complete calls — the runtime-managed candidate did NOT
        # attempt the skip_model_auth retry (would have been a 3rd call).
        assert len(deps.complete_calls) == 2

    def test_backoff_schedule_applied_between_candidates(self) -> None:
        """Backoff is applied between failed candidate attempts.

        Mock the sleep hook to capture the requested wait durations.
        """
        sleeps: list[float] = []
        deps = _FakeDeps(
            [
                {"status": 401, "message": "unauthorized"},
                {"status": 401, "message": "unauthorized"},
                _content("success"),
            ],
            api_keys={
                ("p1", "m1", True): None,
                ("p2", "m2", True): None,
            },
        )
        s = _make_summarizer(
            deps,
            candidates=[("p1", "m1"), ("p2", "m2"), ("p3", "m3")],
            sleep=lambda secs: sleeps.append(secs),
        )
        s.summarize("hello world")
        # Index 0 → 500ms, index 1 → 1000ms.
        assert sleeps == [0.5, 1.0]

    def test_backoff_capped_at_8000ms(self) -> None:
        """A 6th+ candidate uses 8000ms (not 16000)."""
        sleeps: list[float] = []
        # 6 candidates all failing initially, 7th succeeds.
        candidates = [(f"p{i}", f"m{i}") for i in range(7)]
        script: list[Any] = [{"status": 401, "message": "unauthorized"} for _ in range(6)] + [
            _content("eventually")
        ]
        api_keys = {(f"p{i}", f"m{i}", True): None for i in range(7)}
        deps = _FakeDeps(script, api_keys=api_keys)
        s = _make_summarizer(
            deps,
            candidates=candidates,
            sleep=lambda secs: sleeps.append(secs),
        )
        s.summarize("hello world")
        # 6 backoffs: 500, 1000, 2000, 4000, 8000, 8000.
        assert sleeps == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]

    def test_envelope_aware_extraction_recovers(self) -> None:
        """Empty content array → envelope-aware extraction picks up output_text."""
        deps = _FakeDeps([
            {"content": [], "output_text": "found in envelope"},
        ])
        s = _make_summarizer(deps, candidates=[("anthropic", "claude-3-7-sonnet")])
        result = s.summarize("hello world")
        assert "found in envelope" in result

    def test_response_failure_falls_to_next_candidate(self) -> None:
        """``finish_reason=error`` → advance to next candidate."""
        deps = _FakeDeps([
            {"finish_reason": "error", "content": []},
            _content("recovered on candidate 2"),
        ])
        s = _make_summarizer(
            deps,
            candidates=[("p1", "m1"), ("p2", "m2")],
        )
        result = s.summarize("hello world")
        assert "recovered on candidate 2" in result


# =============================================================================
# Backwards-compat: marker-form LcmProviderAuthError
# =============================================================================


class TestLcmProviderAuthErrorBackCompat:
    """The 04-07 marker form of LcmProviderAuthError must still work after 04-06."""

    def test_marker_form_construction(self) -> None:
        """Old construct ``LcmProviderAuthError("msg")`` still works."""
        exc = LcmProviderAuthError("auth fail")
        assert str(exc) == "auth fail"
        assert exc.provider == "(unknown)"
        assert exc.model == "(unknown)"

    def test_full_form_construction(self) -> None:
        """New kwarg form produces TS-equivalent warning text."""
        exc = LcmProviderAuthError(
            provider="anthropic",
            model="claude-3-7-sonnet",
            failure=ProviderAuthFailure(status_code=401, message="bad key"),
        )
        assert "anthropic/claude-3-7-sonnet" in str(exc)
        assert "401" in str(exc)
        assert exc.provider == "anthropic"
        assert exc.model == "claude-3-7-sonnet"


# =============================================================================
# Auth-short-circuit Wave-N provenance comment
# =============================================================================


def test_auth_short_circuit_wave_provenance_comment_present() -> None:
    """Per ADR-029, the auth-short-circuit invariant carries an inline
    Wave-N-style comment block citing TS source.

    Spec text (04-06):

        # LCM auth-short-circuit: if all candidates auth-fail, raise rather than
        # returning deterministic fallback. Caller skips persistence to preserve
        # DAG integrity through transient provider outages.
        # Original: lossless-claw/src/summarize.ts:1665-1685 (final-throw path).
    """
    import pathlib

    import lossless_hermes.summarize as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    assert "auth-short-circuit" in src, (
        "Auth-short-circuit Wave-N invariant comment missing from "
        "src/lossless_hermes/summarize.py — see issue 04-06 spec."
    )
    assert "summarize.ts:1665" in src, (
        "Auth-short-circuit TS-source citation missing from "
        "src/lossless_hermes/summarize.py — ADR-029 requires a "
        "lossless-claw/src/summarize.ts:1665-1685 reference."
    )


def test_module_uses_estimate_tokens_for_target_resolution() -> None:
    """Smoke check: ADR-021 token estimator is imported (not naive char/4)."""
    import pathlib

    import lossless_hermes.summarize as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    assert "from lossless_hermes.estimate_tokens import estimate_tokens" in src
