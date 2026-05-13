"""Tests for :mod:`lossless_hermes.plugin.result_budget` (issue 06-03).

Coverage:

* Env override resolution at module load.
* Floor enforcement (``LCM_TOOL_RESULT_TOKEN_BUDGET=500`` clamps up to
  2000, not down).
* :func:`apply_result_budget_config` raises the cap when env is unset.
* :func:`apply_result_budget_config` is a no-op when env IS set
  (env wins over config).
* :func:`truncation_notice` includes the load-bearing regex
  ``r"truncated at ~\\d+ tokens to protect agent context"``.
* :func:`get_max_result_chars` / :func:`get_max_result_tokens` track
  the live module bindings.

References:

* :mod:`lossless_hermes.plugin.result_budget` — implementation.
* ``/Volumes/LEXAR/Claude/lossless-claw/src/plugin/result-budget.ts`` —
  TS source (LCM commit ``1f07fbd``).
* Issue spec: ``epics/06-tools/06-03-runwithtokengate-middleware.md``.
"""

from __future__ import annotations

import importlib
import re
import typing

import pytest

if typing.TYPE_CHECKING:
    pass  # avoid import-at-module-load: env is mutated by tests

from lossless_hermes.plugin import result_budget


@pytest.fixture(autouse=True)
def _reset_budget_state() -> typing.Iterator[None]:
    """Reset module-level bindings between tests."""
    result_budget.__reset_result_budget_for_testing()
    yield
    result_budget.__reset_result_budget_for_testing()


class TestFloorAndDefault:
    """The floor + default constants are stable knobs."""

    def test_floor_tokens(self) -> None:
        """Floor is 2000 tokens (8K chars at 4 chars/token)."""
        assert result_budget.FLOOR_TOKENS == 2_000

    def test_default_tokens(self) -> None:
        """Default is 10000 tokens."""
        assert result_budget.DEFAULT_TOKENS == 10_000

    def test_chars_per_token(self) -> None:
        """4 chars/token matches the TS convention."""
        assert result_budget.CHARS_PER_TOKEN == 4

    def test_env_var_name(self) -> None:
        """Env var name matches the TS contract."""
        assert result_budget.ENV_VAR_NAME == "LCM_TOOL_RESULT_TOKEN_BUDGET"


class TestEnvResolution:
    """Env-driven budget resolution (module-load).

    The module reads the env var at IMPORT time so we must reload it to
    pick up monkeypatched env. The reload pattern is fragile but
    matches how the TS code behaves with ESM live bindings.
    """

    def test_env_override_takes_effect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``LCM_TOOL_RESULT_TOKEN_BUDGET=20000`` raises the cap to 20K."""
        monkeypatch.setenv("LCM_TOOL_RESULT_TOKEN_BUDGET", "20000")
        importlib.reload(result_budget)
        try:
            assert result_budget.MAX_RESULT_TOKENS == 20_000
            assert result_budget.MAX_RESULT_CHARS == 20_000 * 4
        finally:
            monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
            importlib.reload(result_budget)

    def test_env_below_floor_clamps_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``LCM_TOOL_RESULT_TOKEN_BUDGET=500`` snaps up to 2000."""
        monkeypatch.setenv("LCM_TOOL_RESULT_TOKEN_BUDGET", "500")
        importlib.reload(result_budget)
        try:
            assert result_budget.MAX_RESULT_TOKENS == 2_000
        finally:
            monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
            importlib.reload(result_budget)

    def test_env_unset_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env => default 10000 (which is above the floor)."""
        monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
        importlib.reload(result_budget)
        try:
            assert result_budget.MAX_RESULT_TOKENS == 10_000
        finally:
            importlib.reload(result_budget)

    def test_env_non_numeric_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Garbage env value falls through to default."""
        monkeypatch.setenv("LCM_TOOL_RESULT_TOKEN_BUDGET", "not-a-number")
        importlib.reload(result_budget)
        try:
            assert result_budget.MAX_RESULT_TOKENS == 10_000
        finally:
            monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
            importlib.reload(result_budget)

    def test_env_zero_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zero / negative env values fall through to default."""
        monkeypatch.setenv("LCM_TOOL_RESULT_TOKEN_BUDGET", "0")
        importlib.reload(result_budget)
        try:
            assert result_budget.MAX_RESULT_TOKENS == 10_000
        finally:
            monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
            importlib.reload(result_budget)


class TestApplyResultBudgetConfig:
    """Plugin-config-driven override raises the cap when env is unset."""

    def test_config_raises_cap_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env + ``apply_result_budget_config(15000)`` => cap=15000."""
        monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
        importlib.reload(result_budget)
        try:
            # Pre-apply: default
            assert result_budget.MAX_RESULT_TOKENS == 10_000
            result_budget.apply_result_budget_config(15_000)
            assert result_budget.MAX_RESULT_TOKENS == 15_000
            assert result_budget.MAX_RESULT_CHARS == 15_000 * 4
            # Accessor helpers track the live binding
            assert result_budget.get_max_result_tokens() == 15_000
            assert result_budget.get_max_result_chars() == 15_000 * 4
        finally:
            importlib.reload(result_budget)

    def test_env_wins_over_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Env=8000 + config=15000 => env wins (apply is a no-op)."""
        monkeypatch.setenv("LCM_TOOL_RESULT_TOKEN_BUDGET", "8000")
        importlib.reload(result_budget)
        try:
            assert result_budget.MAX_RESULT_TOKENS == 8_000
            result_budget.apply_result_budget_config(15_000)
            # Env wins; config no-ops.
            assert result_budget.MAX_RESULT_TOKENS == 8_000
            assert result_budget.MAX_RESULT_CHARS == 8_000 * 4
        finally:
            monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
            importlib.reload(result_budget)

    def test_config_below_floor_clamps_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Plugin config below floor still clamps up to 2000."""
        monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
        importlib.reload(result_budget)
        try:
            result_budget.apply_result_budget_config(500)
            assert result_budget.MAX_RESULT_TOKENS == 2_000
        finally:
            importlib.reload(result_budget)

    def test_config_none_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``apply_result_budget_config(None)`` is a no-op."""
        monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
        importlib.reload(result_budget)
        try:
            result_budget.apply_result_budget_config(None)
            assert result_budget.MAX_RESULT_TOKENS == 10_000
        finally:
            importlib.reload(result_budget)

    def test_config_zero_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``apply_result_budget_config(0)`` is a no-op (treated as invalid)."""
        monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
        importlib.reload(result_budget)
        try:
            result_budget.apply_result_budget_config(0)
            assert result_budget.MAX_RESULT_TOKENS == 10_000
        finally:
            importlib.reload(result_budget)

    def test_config_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calling apply repeatedly re-applies the same value."""
        monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
        importlib.reload(result_budget)
        try:
            result_budget.apply_result_budget_config(12_000)
            assert result_budget.MAX_RESULT_TOKENS == 12_000
            result_budget.apply_result_budget_config(12_000)
            assert result_budget.MAX_RESULT_TOKENS == 12_000
        finally:
            importlib.reload(result_budget)


class TestTruncationNotice:
    """The truncation prose is part of the agent-facing contract."""

    def test_includes_load_bearing_regex(self) -> None:
        """The notice MUST match ``r"truncated at ~\\d+ tokens to protect agent context"``.

        Pinned by Wave-12 N3 retro. Tool description prose documents
        this regex to agents; cosmetic edits will silently break the
        agent contract.
        """
        notice = result_budget.truncation_notice("narrow your query")
        assert re.search(r"truncated at ~\d+ tokens to protect agent context", notice)

    def test_includes_reason_hint(self) -> None:
        """The reason_hint appears verbatim in the notice."""
        notice = result_budget.truncation_notice("lower limit")
        assert "lower limit" in notice

    def test_token_count_matches_current_cap(self) -> None:
        """The ~N token count matches :data:`MAX_RESULT_TOKENS`."""
        notice = result_budget.truncation_notice("any reason")
        match = re.search(r"~(\d+) tokens", notice)
        assert match is not None
        assert int(match.group(1)) == result_budget.MAX_RESULT_TOKENS

    def test_includes_operator_hint(self) -> None:
        """The notice tells operators how to raise the cap."""
        notice = result_budget.truncation_notice("ignored")
        assert "LCM_TOOL_RESULT_TOKEN_BUDGET" in notice

    def test_notice_tracks_live_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When apply_result_budget_config raises the cap, the notice updates."""
        monkeypatch.delenv("LCM_TOOL_RESULT_TOKEN_BUDGET", raising=False)
        importlib.reload(result_budget)
        try:
            result_budget.apply_result_budget_config(20_000)
            notice = result_budget.truncation_notice("reason")
            assert "~20000 tokens" in notice
        finally:
            importlib.reload(result_budget)


class TestTruncationNoticeFormatConstant:
    """:data:`TRUNCATION_NOTICE_FORMAT` is exported for callers that need it."""

    def test_constant_is_exported(self) -> None:
        """The format-string constant is part of the public surface."""
        assert hasattr(result_budget, "TRUNCATION_NOTICE_FORMAT")
        assert isinstance(result_budget.TRUNCATION_NOTICE_FORMAT, str)

    def test_constant_has_load_bearing_substring(self) -> None:
        """The constant carries the regex-pinned substring template."""
        # The regex pattern is ``r"truncated at ~\d+ tokens to protect agent context"``.
        # The format-string contains the literal text (with ``{tokens}`` placeholder).
        assert "truncated at ~{tokens} tokens to protect agent context" in (
            result_budget.TRUNCATION_NOTICE_FORMAT
        )
