"""Tests for :mod:`lossless_hermes.synthesis.tier_routing` (issue 07-10).

Per ADR-031, v0.1 ships Option A (match TS exactly). These tests pin:

1. The public re-exports (:data:`SYNTHESIS_TIER_DEFAULTS`,
   :data:`SYNTHESIS_TIER_PASS_STRATEGIES`) are the same objects as
   :mod:`dispatch`'s tables — so a runtime override that mutates the
   underlying dispatch table is visible through the public surface.
2. :func:`resolve_default_model_from_env` reads
   :data:`LCM_SUMMARY_MODEL_ENV` at call time with the documented
   trimming + fallback semantics (parity with TS at
   :file:`lossless-claw/src/synthesis/dispatch.ts:71`).
3. :func:`pick_synthesis_model` mirrors the TS
   :func:`pickModel` precedence at ``dispatch.ts:755-766``:

   1. ``force_model`` AND ``model_override`` → ``model_override``.
   2. ``force_model`` alone → :data:`SYNTHESIS_TIER_DEFAULTS` for the
      tier (Wave-4 Auditor #5 P1 regression).
   3. otherwise → ``prompt.model_recommendation`` or ``model_override``
      or :data:`SYNTHESIS_TIER_DEFAULTS` for the tier.

4. :data:`TIER_LADDER_DEFERRED` is a non-empty string referencing
   ADR-031, so a future ``grep -rn "TIER_LADDER_DEFERRED" src/``
   enumerates the deferral marker.

Per ADR-031, no new behavioural surface is added — the precedence
matrix is the same as :mod:`dispatch`'s :func:`_pick_model`. The tests
here verify the public wrapper does not drift from the private impl.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from lossless_hermes.synthesis import dispatch as dispatch_module
from lossless_hermes.synthesis import tier_routing
from lossless_hermes.synthesis.dispatch import (
    DEFAULT_MODEL_BY_TIER,
    PASS_STRATEGY_BY_TIER,
    SynthesizeRequest,
    TierLabel,
)
from lossless_hermes.synthesis.tier_routing import (
    DEFAULT_SUMMARY_MODEL_FALLBACK,
    LCM_SUMMARY_MODEL_ENV,
    SYNTHESIS_TIER_DEFAULTS,
    SYNTHESIS_TIER_PASS_STRATEGIES,
    TIER_LADDER_DEFERRED,
    pick_synthesis_model,
    resolve_default_model_from_env,
)
from lossless_hermes.synthesis.types import PromptRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_prompt(model_recommendation: str | None = None) -> PromptRecord:
    """Build a minimal :class:`PromptRecord` for the precedence tests.

    All other fields use placeholder values; only ``model_recommendation``
    drives the resolver under test.
    """

    return PromptRecord(
        prompt_id="pr_test01",
        memory_type="episodic-condensed",
        tier_label="daily",
        pass_kind="single",
        version=1,
        template="x",
        model_recommendation=model_recommendation,
        created_at="2026-05-14T00:00:00Z",
        active=True,
        bundle_version=1,
        notes=None,
    )


def _make_req(
    tier: TierLabel = "daily",
    *,
    model_override: str | None = None,
    force_model: bool = False,
) -> SynthesizeRequest:
    """Build a minimal :class:`SynthesizeRequest` for the precedence tests."""

    return SynthesizeRequest(
        tier=tier,
        memory_type="episodic-condensed",
        source_text="x",
        pass_session_id="ps-test",
        target_summary_id="sum_t",
        model_override=model_override,
        force_model=force_model,
    )


@pytest.fixture
def clean_env() -> Iterator[None]:
    """Snapshot + restore :data:`LCM_SUMMARY_MODEL_ENV` across tests.

    Tests that mutate the env var to verify
    :func:`resolve_default_model_from_env` semantics must not leak the
    mutation to sibling tests. The fixture is preferred over
    ``monkeypatch`` because we want the snapshot to include an
    explicitly-unset state.
    """

    original = os.environ.get(LCM_SUMMARY_MODEL_ENV)
    try:
        if LCM_SUMMARY_MODEL_ENV in os.environ:
            del os.environ[LCM_SUMMARY_MODEL_ENV]
        yield
    finally:
        if original is None:
            os.environ.pop(LCM_SUMMARY_MODEL_ENV, None)
        else:
            os.environ[LCM_SUMMARY_MODEL_ENV] = original


# ---------------------------------------------------------------------------
# TestPublicReexports
# ---------------------------------------------------------------------------


class TestPublicReexports:
    """Public alias tables identity-match dispatch's tables.

    The ``Final`` annotations on :data:`SYNTHESIS_TIER_DEFAULTS` and
    :data:`SYNTHESIS_TIER_PASS_STRATEGIES` are documentation-only at
    runtime; the underlying objects are dispatch's own tables. We pin
    the identity so a downstream caller mutating the public surface
    (or the dispatch surface) sees the change everywhere.
    """

    def test_tier_defaults_is_dispatch_table(self) -> None:
        assert SYNTHESIS_TIER_DEFAULTS is DEFAULT_MODEL_BY_TIER

    def test_pass_strategies_is_dispatch_table(self) -> None:
        assert SYNTHESIS_TIER_PASS_STRATEGIES is PASS_STRATEGY_BY_TIER

    def test_tier_defaults_covers_all_six_tiers(self) -> None:
        """Parity with :class:`TestConstants.test_default_model_by_tier_covers_all_tiers`."""
        for tier in ("daily", "weekly", "monthly", "yearly", "custom", "filtered"):
            assert tier in SYNTHESIS_TIER_DEFAULTS
            assert SYNTHESIS_TIER_DEFAULTS[tier]

    def test_pass_strategies_match_porting_guide_table(self) -> None:
        """Parity with porting-guide §"Pass strategy by tier" table."""
        assert SYNTHESIS_TIER_PASS_STRATEGIES["daily"] == ["single"]
        assert SYNTHESIS_TIER_PASS_STRATEGIES["weekly"] == ["single"]
        assert SYNTHESIS_TIER_PASS_STRATEGIES["monthly"] == ["single", "verify_fidelity"]
        assert SYNTHESIS_TIER_PASS_STRATEGIES["yearly"] == ["best_of_n_judge"]
        assert SYNTHESIS_TIER_PASS_STRATEGIES["custom"] == ["single"]
        assert SYNTHESIS_TIER_PASS_STRATEGIES["filtered"] == ["single"]

    def test_option_a_all_tiers_share_one_default(self) -> None:
        """ADR-031 Option A: every tier resolves to the same model at import.

        The TS source populates every tier with the same
        ``_LCM_DEFAULT_MODEL`` value. We pin this identity here so that a
        future regression that introduces tier-specific defaults at
        dispatch-import time (without an ADR superseding 031) fails this
        test loudly.
        """
        values = set(SYNTHESIS_TIER_DEFAULTS.values())
        assert len(values) == 1, (
            f"ADR-031 expects all tiers to share one default model; saw multiple values: {values!r}"
        )


# ---------------------------------------------------------------------------
# TestResolveDefaultModelFromEnv
# ---------------------------------------------------------------------------


class TestResolveDefaultModelFromEnv:
    """:func:`resolve_default_model_from_env` reads env at call time.

    Parity with the TS expression at
    :file:`lossless-claw/src/synthesis/dispatch.ts:71`:

    .. code-block:: ts

        const _LCM_DEFAULT_MODEL =
            process.env.LCM_SUMMARY_MODEL?.trim() || "gpt-5.4-mini";
    """

    def test_unset_env_returns_fallback(self, clean_env: None) -> None:
        assert LCM_SUMMARY_MODEL_ENV not in os.environ
        assert resolve_default_model_from_env() == DEFAULT_SUMMARY_MODEL_FALLBACK
        assert resolve_default_model_from_env() == "gpt-5.4-mini"

    def test_blank_env_returns_fallback(self, clean_env: None) -> None:
        os.environ[LCM_SUMMARY_MODEL_ENV] = ""
        assert resolve_default_model_from_env() == DEFAULT_SUMMARY_MODEL_FALLBACK

    def test_whitespace_only_env_returns_fallback(self, clean_env: None) -> None:
        """TS ``?.trim() || fallback`` parity — `"   "` → fallback."""
        os.environ[LCM_SUMMARY_MODEL_ENV] = "   "
        assert resolve_default_model_from_env() == DEFAULT_SUMMARY_MODEL_FALLBACK

    def test_set_env_returns_trimmed(self, clean_env: None) -> None:
        os.environ[LCM_SUMMARY_MODEL_ENV] = "  claude-sonnet-4  "
        assert resolve_default_model_from_env() == "claude-sonnet-4"

    def test_set_env_returns_verbatim(self, clean_env: None) -> None:
        os.environ[LCM_SUMMARY_MODEL_ENV] = "claude-opus-4"
        assert resolve_default_model_from_env() == "claude-opus-4"

    def test_call_time_reads_current_env(self, clean_env: None) -> None:
        """Distinct from :data:`SYNTHESIS_TIER_DEFAULTS` which freezes at import."""
        os.environ[LCM_SUMMARY_MODEL_ENV] = "first"
        assert resolve_default_model_from_env() == "first"
        os.environ[LCM_SUMMARY_MODEL_ENV] = "second"
        assert resolve_default_model_from_env() == "second"

    def test_call_time_drift_from_import_time_default(self, clean_env: None) -> None:
        """The dispatch table keeps its import-time value; the env reader
        sees live mutations.

        This is the documented divergence point — health-check callers
        should prefer :func:`resolve_default_model_from_env` when they
        want "what would dispatch pick now if a fresh process started"
        rather than "what did dispatch pick at plugin load."
        """
        os.environ[LCM_SUMMARY_MODEL_ENV] = "live-only-value"
        # The dispatch table was frozen at import; the env reader sees
        # the current value. They MAY disagree.
        assert resolve_default_model_from_env() == "live-only-value"
        # The dispatch table value is whatever was set at import (could
        # be the fallback or whatever was in env at the time of test
        # collection). We just verify the reader is decoupled.
        assert resolve_default_model_from_env() != "definitely-not-a-frozen-default"


# ---------------------------------------------------------------------------
# TestPickSynthesisModel
# ---------------------------------------------------------------------------


class TestPickSynthesisModel:
    """:func:`pick_synthesis_model` precedence parity with dispatch's ``_pick_model``.

    Same precedence matrix as
    :class:`tests.synthesis.test_dispatch.TestModelResolution` +
    :class:`TestParityChecklist.test_2_force_model_no_override_uses_tier_default`,
    but exercised through the public wrapper to verify it does not
    drift from the private implementation.

    TS source: :file:`lossless-claw/src/synthesis/dispatch.ts:755-766`.
    """

    def test_force_model_with_override_returns_override(self) -> None:
        """Precedence 1: ``force_model`` AND ``model_override`` → ``model_override``."""
        req = _make_req(model_override="force-this", force_model=True)
        prompt = _make_prompt(model_recommendation="should-not-be-used")
        assert pick_synthesis_model(req, prompt) == "force-this"

    def test_force_model_without_override_returns_tier_default(self) -> None:
        """Precedence 2 (Wave-4 Auditor #5 P1): ``force_model`` alone →
        :data:`SYNTHESIS_TIER_DEFAULTS` for tier, NOT prompt's
        ``model_recommendation``.

        This is the exact regression behaviour pinned by
        :meth:`tests.synthesis.test_dispatch.TestParityChecklist.test_2_force_model_no_override_uses_tier_default`
        — duplicated here against the public wrapper.
        """
        req = _make_req(tier="daily", model_override=None, force_model=True)
        prompt = _make_prompt(model_recommendation="should-NOT-be-used-because-force-model")
        assert pick_synthesis_model(req, prompt) == SYNTHESIS_TIER_DEFAULTS["daily"]

    def test_prompt_recommendation_overrides_tier_default(self) -> None:
        """Precedence 3a: ``prompt.model_recommendation`` wins when no force."""
        req = _make_req(model_override=None, force_model=False)
        prompt = _make_prompt(model_recommendation="specific-model")
        assert pick_synthesis_model(req, prompt) == "specific-model"

    def test_prompt_recommendation_wins_over_model_override(self) -> None:
        """Precedence 3a: ``prompt.model_recommendation`` wins over
        ``model_override`` when ``force_model`` is False.

        This is the order documented in :func:`_pick_model`:
        ``prompt.model_recommendation OR model_override OR
        DEFAULT_MODEL_BY_TIER[tier]``.
        """
        req = _make_req(model_override="not-this-one", force_model=False)
        prompt = _make_prompt(model_recommendation="this-one")
        assert pick_synthesis_model(req, prompt) == "this-one"

    def test_model_override_wins_over_tier_default_when_no_prompt_rec(self) -> None:
        """Precedence 3b: ``model_override`` wins over tier default when no prompt rec."""
        req = _make_req(tier="weekly", model_override="override-model", force_model=False)
        prompt = _make_prompt(model_recommendation=None)
        assert pick_synthesis_model(req, prompt) == "override-model"

    def test_tier_default_when_no_recommendation_no_override(self) -> None:
        """Precedence 3c: tier default when no recommendation, no override."""
        req = _make_req(tier="yearly", model_override=None, force_model=False)
        prompt = _make_prompt(model_recommendation=None)
        assert pick_synthesis_model(req, prompt) == SYNTHESIS_TIER_DEFAULTS["yearly"]

    @pytest.mark.parametrize(
        "tier",
        ["daily", "weekly", "monthly", "yearly", "custom", "filtered"],
    )
    def test_all_tiers_resolve_under_option_a_defaults(self, tier: TierLabel) -> None:
        """Option A: every tier resolves to the same default value when no
        prompt recommendation + no override + no force.

        Pins the ADR-031 invariant. If a future PR seeds tier-specific
        defaults at dispatch-import time without an ADR supersede,
        this test catches it.
        """
        req = _make_req(tier=tier, model_override=None, force_model=False)
        prompt = _make_prompt(model_recommendation=None)
        assert pick_synthesis_model(req, prompt) == SYNTHESIS_TIER_DEFAULTS[tier]

    def test_wraps_dispatch_pick_model(self) -> None:
        """The wrapper delegates to dispatch's :func:`_pick_model` verbatim.

        Calling :func:`pick_synthesis_model` MUST yield the same result
        as the private :func:`_pick_model` for the same inputs. We
        verify by direct comparison.
        """
        req = _make_req(tier="monthly", model_override="ov", force_model=False)
        prompt = _make_prompt(model_recommendation="rec")
        assert pick_synthesis_model(req, prompt) == dispatch_module._pick_model(req, prompt)


# ---------------------------------------------------------------------------
# TestTierLadderDeferredMarker
# ---------------------------------------------------------------------------


class TestTierLadderDeferredMarker:
    """:data:`TIER_LADDER_DEFERRED` is a discoverable forward-reference marker."""

    def test_marker_is_non_empty_string(self) -> None:
        assert isinstance(TIER_LADDER_DEFERRED, str)
        assert len(TIER_LADDER_DEFERRED) > 0

    def test_marker_references_adr_031(self) -> None:
        """The deferral marker MUST link readers to ADR-031.

        Pins the inline-marker pattern (parallel to ADR-029's Wave-N
        comments) so that a future contributor running
        ``grep -rn TIER_LADDER_DEFERRED src/`` lands on the ADR that
        explains why every tier ships with the same default.
        """
        assert "ADR-031" in TIER_LADDER_DEFERRED

    def test_marker_references_register_prompt_override_path(self) -> None:
        """Marker doubles as inline documentation for the operator override path."""
        assert "register_prompt" in TIER_LADDER_DEFERRED


# ---------------------------------------------------------------------------
# TestSeedNullModelRecommendation (Option A enforcement at seed-data level)
# ---------------------------------------------------------------------------


class TestSeedNullModelRecommendation:
    """Per ADR-031 Option A: every default-seeded prompt has
    ``model_recommendation = None``.

    Defends the invariant from regressions where a future PR adds a
    seeded ladder without an ADR supersede. If Option B/C ever ships,
    a new ADR (032+) will explicitly call out this test as superseded
    and the test will be updated alongside the seed change.
    """

    def test_every_seeded_prompt_has_null_model_recommendation(self) -> None:
        from lossless_hermes.synthesis.seed_prompts import DEFAULT_PROMPTS

        offenders = [p for p in DEFAULT_PROMPTS if p.model_recommendation is not None]
        assert offenders == [], (
            "ADR-031 Option A requires every seeded default prompt to ship "
            "with model_recommendation = None. Found seeded rows with non-None "
            f"recommendations: {offenders!r}. If shipping Option B or C, write "
            "a new ADR superseding ADR-031 and update this test."
        )


# ---------------------------------------------------------------------------
# TestImportTimeEnvResolution (parity with TS module-load semantics)
# ---------------------------------------------------------------------------


class TestImportTimeEnvResolution:
    """Verify dispatch's tier-defaults table reflects the env value at
    module-import time.

    TS source semantics: ``const _LCM_DEFAULT_MODEL = process.env.LCM_SUMMARY_MODEL?.trim() || "gpt-5.4-mini"``
    runs once at module load. We reproduce this by reloading the
    dispatch module with a controlled env, then asserting the public
    surface reflects the value.

    NOTE: reloading is scoped to this single test; we restore the
    module afterwards so the rest of the suite sees the original
    import state.
    """

    def test_reload_with_env_set_propagates_to_tier_defaults(self) -> None:
        sentinel = "test-sentinel-model-name-1f07fbd"
        with patch.dict(os.environ, {LCM_SUMMARY_MODEL_ENV: sentinel}, clear=False):
            # Reload dispatch so it re-reads the env at module load
            reloaded_dispatch = importlib.reload(dispatch_module)
            try:
                assert reloaded_dispatch.DEFAULT_MODEL_BY_TIER["daily"] == sentinel
                assert reloaded_dispatch.DEFAULT_MODEL_BY_TIER["yearly"] == sentinel
                # All tiers share the same value (ADR-031 Option A)
                assert len(set(reloaded_dispatch.DEFAULT_MODEL_BY_TIER.values())) == 1
            finally:
                # Restore the original module state for sibling tests by
                # reloading without the sentinel env. The cleanup is
                # idempotent: another reload picks up whatever env was
                # present before this test started.
                importlib.reload(dispatch_module)
                importlib.reload(tier_routing)
