"""Comprehensive tests for ``lossless_hermes.db.config`` (issue 01-02).

Ports the 61-case ``test/config.test.ts`` from the LCM source (commit
``1f07fbd``) to pytest, plus adds Python-specific coverage:

* Voyage credential three-tier resolver per ADR-022.
* ``LCM_*`` env-var alias DeprecationWarning emission (Phase-1 policy).
* ``extra='forbid'`` rejecting unknown keys.
* Validation errors on invalid values (negative thresholds, bad enums).
* Table-driven default-value parity check (every TS default → Python).

The test file is organized to mirror the TS describe blocks:

1. ``resolve_lcm_config`` precedence + defaults (the bulk).
2. ``resolve_lcm_config_with_diagnostics`` pattern-array source tracking.
3. ``resolve_openclaw_state_dir`` + ``resolve_hermes_state_dir``.
4. ``resolve_voyage_api_key`` three-tier (Python-specific).
5. ``LcmConfig`` model validation (extra=forbid, ge/le).
6. Env-var alias deprecation warnings (Phase-1 policy).
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lossless_hermes.db.config import (
    DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES,
    DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO,
    LcmConfig,
    LcmConfigDiagnostics,
    _emit_lcm_deprecation_warning,
    describe_lcm_config_source,
    resolve_hermes_state_dir,
    resolve_lcm_config,
    resolve_lcm_config_with_diagnostics,
    resolve_openclaw_state_dir,
    resolve_voyage_api_key,
)


@pytest.fixture(autouse=True)
def _reset_deprecation_cache() -> None:
    """Each test starts with a clean deprecation-warning cache so
    deprecation assertions are independent."""
    _emit_lcm_deprecation_warning.cache_clear()


# ===========================================================================
# 1. resolve_lcm_config — TS-equivalent precedence + defaults
# ===========================================================================


class TestResolveLcmConfigDefaults:
    """Table-driven check: every TS default must mirror in Python.

    Source of truth: ``src/db/config.ts:448-606`` + the docstring of
    every field on :class:`LcmConfig`. If a default drifts, this
    test fails with a clear pointer to the unmatched value.
    """

    def test_hardcoded_defaults_when_no_env_or_plugin_config(self) -> None:
        """Mirror of TS ``it("uses hardcoded defaults when no env or plugin config")``."""
        config = resolve_lcm_config({}, {})
        assert config.enabled is True
        assert config.database_path == str(Path.home() / ".openclaw" / "lcm.db")
        assert config.large_files_dir == str(Path.home() / ".openclaw" / "lcm-files")
        assert config.ignore_session_patterns == []
        assert config.stateless_session_patterns == []
        assert config.skip_stateless_sessions is True
        assert config.context_threshold == 0.75
        assert config.fresh_tail_count == 64
        assert config.fresh_tail_max_tokens is None
        assert config.prompt_aware_eviction is False
        assert config.new_session_retain_depth == 2
        assert config.incremental_max_depth == 1
        assert config.leaf_chunk_tokens == 20000
        assert config.leaf_min_fanout == 8
        assert config.condensed_min_fanout == 4
        assert config.condensed_min_fanout_hard == 2
        assert config.leaf_target_tokens == 4000  # v4.1 (A.10): raised from 2400
        assert config.summary_provider == ""
        assert config.summary_model == ""
        assert config.prune_heartbeat_ok is False
        assert config.transcript_gc_enabled is False
        assert config.proactive_threshold_compaction_mode == "deferred"
        assert config.auto_rotate_session_files.model_dump() == {
            "enabled": True,
            "size_bytes": DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES,
            "startup": "rotate",
            "runtime": "rotate",
        }
        assert config.cache_aware_compaction.model_dump() == {
            "enabled": True,
            "cache_ttl_seconds": 300,
            "max_cold_cache_catchup_passes": 2,
            "hot_cache_pressure_factor": 4.0,
            "hot_cache_budget_headroom_ratio": 0.2,
            "cold_cache_observation_threshold": 3,
            "critical_budget_pressure_ratio": DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO,
        }
        assert config.dynamic_leaf_chunk_tokens.model_dump() == {
            "enabled": True,
            "max": 40000,
        }

    def test_default_summary_max_overage_factor_and_max_assembly_token_budget(self) -> None:
        """Mirror of TS ``defaults summaryMaxOverageFactor to 3 and maxAssemblyTokenBudget to undefined``."""
        config = resolve_lcm_config({}, {})
        assert config.bootstrap_max_tokens == 6000
        assert config.delegation_timeout_ms == 120000
        assert config.summary_max_overage_factor == 3.0
        assert config.max_assembly_token_budget is None

    def test_expansion_model_and_provider_default_to_empty_string(self) -> None:
        """Mirror of TS ``defaults expansionModel and expansionProvider to empty string``."""
        config = resolve_lcm_config({}, {})
        assert config.expansion_model == ""
        assert config.expansion_provider == ""

    def test_summary_overrides_default_to_empty_strings(self) -> None:
        """Mirror of TS ``defaults summary overrides to empty strings when unset``."""
        config = resolve_lcm_config({}, {"freshTailCount": 16})
        assert config.summary_provider == ""
        assert config.summary_model == ""


class TestResolveLcmConfigFromPluginConfig:
    """Plugin-config wins when env is unset. Mirrors TS plugin-config
    test cases."""

    def test_reads_values_from_plugin_config(self) -> None:
        """Mirror of TS ``reads values from plugin config``."""
        config = resolve_lcm_config(
            {},
            {
                "contextThreshold": 0.5,
                "freshTailCount": 16,
                "freshTailMaxTokens": 12000,
                "promptAwareEviction": False,
                "leafChunkTokens": 80000,
                "newSessionRetainDepth": 3,
                "incrementalMaxDepth": -1,
                "ignoreSessionPatterns": ["agent:*:cron:*", "agent:main:subagent:**"],
                "statelessSessionPatterns": ["agent:*:ephemeral:**"],
                "skipStatelessSessions": False,
                "leafMinFanout": 4,
                "condensedMinFanout": 2,
                "pruneHeartbeatOk": True,
                "transcriptGcEnabled": True,
                "proactiveThresholdCompactionMode": "inline",
                "autoRotateSessionFiles": {
                    "enabled": False,
                    "sizeBytes": 123456,
                    "startup": "warn",
                    "runtime": "off",
                },
                "enabled": False,
                "cacheAwareCompaction": {
                    "enabled": False,
                    "cacheTTLSeconds": 900,
                    "maxColdCacheCatchupPasses": 3,
                    "hotCachePressureFactor": 6,
                    "hotCacheBudgetHeadroomRatio": 0.35,
                    "coldCacheObservationThreshold": 4,
                },
                "dynamicLeafChunkTokens": {
                    "enabled": True,
                    "max": 50000,
                },
            },
        )
        assert config.enabled is False
        assert config.ignore_session_patterns == [
            "agent:*:cron:*",
            "agent:main:subagent:**",
        ]
        assert config.stateless_session_patterns == ["agent:*:ephemeral:**"]
        assert config.skip_stateless_sessions is False
        assert config.context_threshold == 0.5
        assert config.fresh_tail_count == 16
        assert config.fresh_tail_max_tokens == 12000
        assert config.prompt_aware_eviction is False
        assert config.new_session_retain_depth == 3
        assert config.leaf_chunk_tokens == 80000
        assert config.incremental_max_depth == -1
        assert config.leaf_min_fanout == 4
        assert config.condensed_min_fanout == 2
        assert config.prune_heartbeat_ok is True
        assert config.transcript_gc_enabled is True
        assert config.proactive_threshold_compaction_mode == "inline"
        assert config.auto_rotate_session_files.model_dump() == {
            "enabled": False,
            "size_bytes": 123456,
            "startup": "warn",
            "runtime": "off",
        }
        assert config.cache_aware_compaction.model_dump() == {
            "enabled": False,
            "cache_ttl_seconds": 900,
            "max_cold_cache_catchup_passes": 3,
            "hot_cache_pressure_factor": 6.0,
            "hot_cache_budget_headroom_ratio": 0.35,
            "cold_cache_observation_threshold": 4,
            "critical_budget_pressure_ratio": DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO,
        }
        assert config.dynamic_leaf_chunk_tokens.model_dump() == {
            "enabled": True,
            "max": 80000,  # clamped up to leaf_chunk_tokens floor
        }

    def test_handles_string_values_in_plugin_config(self) -> None:
        """Mirror of TS ``handles string values in plugin config (from JSON)``.

        Plugin config arriving from JSON often has stringified numbers.
        ``_to_number`` / ``_to_bool`` coerce gracefully.
        """
        config = resolve_lcm_config(
            {},
            {
                "contextThreshold": "0.6",
                "freshTailCount": "24",
                "freshTailMaxTokens": "4800",
                "promptAwareEviction": "false",
                "leafChunkTokens": "64000",
                "newSessionRetainDepth": "6",
                "ignoreSessionPatterns": "agent:*:cron:*, agent:main:subagent:**",
                "statelessSessionPatterns": "agent:*:ephemeral:**, agent:main:preview:*",
                "skipStatelessSessions": "false",
                "autoRotateSessionFiles": {
                    "enabled": "false",
                    "sizeBytes": "4096",
                    "startup": "warn",
                    "runtime": "off",
                },
            },
        )
        assert config.context_threshold == 0.6
        assert config.fresh_tail_count == 24
        assert config.fresh_tail_max_tokens == 4800
        assert config.prompt_aware_eviction is False
        assert config.new_session_retain_depth == 6
        assert config.leaf_chunk_tokens == 64000
        assert config.ignore_session_patterns == [
            "agent:*:cron:*",
            "agent:main:subagent:**",
        ]
        assert config.stateless_session_patterns == [
            "agent:*:ephemeral:**",
            "agent:main:preview:*",
        ]
        assert config.skip_stateless_sessions is False
        assert config.auto_rotate_session_files.model_dump() == {
            "enabled": False,
            "size_bytes": 4096,
            "startup": "warn",
            "runtime": "off",
        }

    def test_ignores_invalid_plugin_config_values(self) -> None:
        """Mirror of TS ``ignores invalid plugin config values``.

        Unparseable numerics / bools fall through to defaults rather
        than crash.
        """
        config = resolve_lcm_config(
            {},
            {
                "contextThreshold": "not-a-number",
                "freshTailCount": None,
                "freshTailMaxTokens": "not-a-number",
                "promptAwareEviction": "maybe",
                "newSessionRetainDepth": "nope",
                "enabled": "maybe",
                "autoRotateSessionFiles": {
                    "enabled": "maybe",
                    "sizeBytes": "not-a-number",
                    "startup": "notify",
                    "runtime": "compact",
                },
            },
        )
        assert config.context_threshold == 0.75  # default
        assert config.fresh_tail_count == 64  # default
        assert config.fresh_tail_max_tokens is None
        assert config.prompt_aware_eviction is False  # default
        assert config.new_session_retain_depth == 2  # default
        assert config.enabled is True  # default
        assert config.auto_rotate_session_files.model_dump() == {
            "enabled": True,
            "size_bytes": DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES,
            "startup": "rotate",
            "runtime": "rotate",
        }

    def test_database_path_from_plugin_config(self) -> None:
        """Mirror of TS ``handles databasePath from plugin config``."""
        config = resolve_lcm_config({}, {"databasePath": "/custom/path/lcm.db"})
        assert config.database_path == "/custom/path/lcm.db"

    def test_database_path_accepts_manifest_db_path_alias(self) -> None:
        """Mirror of TS ``accepts manifest dbPath from plugin config``.

        The TS manifest historically used ``dbPath`` (now renamed to
        ``databasePath``). The resolver accepts both for back-compat.
        """
        config = resolve_lcm_config({}, {"dbPath": "/manifest/path/lcm.db"})
        assert config.database_path == "/manifest/path/lcm.db"

    def test_large_files_dir_from_plugin_config(self) -> None:
        """Mirror of TS ``handles largeFilesDir from plugin config``."""
        config = resolve_lcm_config({}, {"largeFilesDir": "/custom/path/lcm-files"})
        assert config.large_files_dir == "/custom/path/lcm-files"

    def test_large_file_token_threshold_alias(self) -> None:
        """Mirror of TS ``accepts manifest largeFileThresholdTokens from plugin config``."""
        config = resolve_lcm_config({}, {"largeFileThresholdTokens": 12345})
        assert config.large_file_token_threshold == 12345

    def test_expansion_model_and_provider_from_plugin_config(self) -> None:
        """Mirror of TS ``reads expansionModel and expansionProvider from plugin config``."""
        config = resolve_lcm_config(
            {},
            {"expansionModel": "anthropic/claude-haiku-4-5", "expansionProvider": "anthropic"},
        )
        assert config.expansion_model == "anthropic/claude-haiku-4-5"
        assert config.expansion_provider == "anthropic"

    def test_delegation_timeout_ms_from_plugin_config(self) -> None:
        """Mirror of TS ``reads delegationTimeoutMs from plugin config``."""
        config = resolve_lcm_config({}, {"delegationTimeoutMs": 300000})
        assert config.delegation_timeout_ms == 300000

    def test_cache_aware_compaction_from_plugin_config(self) -> None:
        """Mirror of TS ``reads cache-aware compaction settings from plugin config``."""
        config = resolve_lcm_config(
            {},
            {
                "cacheAwareCompaction": {
                    "enabled": False,
                    "cacheTTLSeconds": 900,
                    "maxColdCacheCatchupPasses": 3,
                    "hotCachePressureFactor": 6,
                    "hotCacheBudgetHeadroomRatio": 0.35,
                    "coldCacheObservationThreshold": 4,
                }
            },
        )
        assert config.cache_aware_compaction.model_dump() == {
            "enabled": False,
            "cache_ttl_seconds": 900,
            "max_cold_cache_catchup_passes": 3,
            "hot_cache_pressure_factor": 6.0,
            "hot_cache_budget_headroom_ratio": 0.35,
            "cold_cache_observation_threshold": 4,
            "critical_budget_pressure_ratio": DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO,
        }

    def test_summary_max_overage_and_max_assembly_token_budget_from_plugin_config(self) -> None:
        """Mirror of TS ``reads summaryMaxOverageFactor and maxAssemblyTokenBudget from plugin config``."""
        config = resolve_lcm_config(
            {}, {"summaryMaxOverageFactor": 5, "maxAssemblyTokenBudget": 30000}
        )
        assert config.summary_max_overage_factor == 5.0
        assert config.max_assembly_token_budget == 30000


class TestResolveLcmConfigEnvOverrides:
    """Env var precedence: env > plugin-config > default."""

    def test_env_vars_override_plugin_config(self) -> None:
        """Mirror of TS ``env vars override plugin config``."""
        env = {
            "LCM_CONTEXT_THRESHOLD": "0.9",
            "LCM_FRESH_TAIL_COUNT": "64",
            "LCM_FRESH_TAIL_MAX_TOKENS": "32000",
            "LCM_PROMPT_AWARE_EVICTION_ENABLED": "false",
            "LCM_NEW_SESSION_RETAIN_DEPTH": "5",
            "LCM_INCREMENTAL_MAX_DEPTH": "3",
            "LCM_ENABLED": "false",
            "LCM_IGNORE_SESSION_PATTERNS": "agent:*:cron:*, agent:main:subagent:**",
            "LCM_STATELESS_SESSION_PATTERNS": "agent:*:ephemeral:**, agent:main:preview:*",
            "LCM_SKIP_STATELESS_SESSIONS": "false",
            "LCM_TRANSCRIPT_GC_ENABLED": "true",
            "LCM_AUTO_ROTATE_SESSION_FILES_ENABLED": "false",
            "LCM_AUTO_ROTATE_SESSION_FILES_SIZE_BYTES": "987654",
            "LCM_AUTO_ROTATE_SESSION_FILES_STARTUP": "warn",
            "LCM_AUTO_ROTATE_SESSION_FILES_RUNTIME": "off",
            "LCM_CACHE_AWARE_COMPACTION_ENABLED": "false",
            "LCM_CACHE_TTL_SECONDS": "600",
            "LCM_MAX_COLD_CACHE_CATCHUP_PASSES": "4",
            "LCM_HOT_CACHE_PRESSURE_FACTOR": "5.5",
            "LCM_HOT_CACHE_BUDGET_HEADROOM_RATIO": "0.25",
            "LCM_COLD_CACHE_OBSERVATION_THRESHOLD": "5",
            "LCM_DYNAMIC_LEAF_CHUNK_TOKENS_ENABLED": "true",
            "LCM_DYNAMIC_LEAF_CHUNK_TOKENS_MAX": "60000",
            "LCM_PROACTIVE_THRESHOLD_COMPACTION_MODE": "inline",
        }
        plugin_config = {
            "contextThreshold": 0.5,
            "freshTailCount": 16,
            "freshTailMaxTokens": 12000,
            "promptAwareEviction": True,
            "incrementalMaxDepth": -1,
            "ignoreSessionPatterns": ["agent:*:test:*"],
            "statelessSessionPatterns": ["agent:*:preview:*"],
            "skipStatelessSessions": True,
            "transcriptGcEnabled": False,
            "proactiveThresholdCompactionMode": "deferred",
            "autoRotateSessionFiles": {
                "enabled": True,
                "sizeBytes": 123456,
                "startup": "rotate",
                "runtime": "rotate",
            },
            "enabled": True,
            "cacheAwareCompaction": {
                "enabled": True,
                "cacheTTLSeconds": 120,
                "maxColdCacheCatchupPasses": 2,
                "hotCachePressureFactor": 3,
                "hotCacheBudgetHeadroomRatio": 0.1,
                "coldCacheObservationThreshold": 2,
            },
            "dynamicLeafChunkTokens": {"enabled": False, "max": 50000},
        }
        # Suppress LCM_* deprecation warnings within this test (they're
        # covered explicitly in TestDeprecationWarnings below).
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(env, plugin_config)
        assert config.enabled is False  # env wins
        assert config.ignore_session_patterns == [
            "agent:*:cron:*",
            "agent:main:subagent:**",
        ]
        assert config.stateless_session_patterns == [
            "agent:*:ephemeral:**",
            "agent:main:preview:*",
        ]
        assert config.skip_stateless_sessions is False
        assert config.transcript_gc_enabled is True
        assert config.proactive_threshold_compaction_mode == "inline"
        assert config.auto_rotate_session_files.model_dump() == {
            "enabled": False,
            "size_bytes": 987654,
            "startup": "warn",
            "runtime": "off",
        }
        assert config.context_threshold == 0.9
        assert config.fresh_tail_count == 64
        assert config.fresh_tail_max_tokens == 32000
        assert config.prompt_aware_eviction is False
        assert config.new_session_retain_depth == 5
        assert config.incremental_max_depth == 3
        assert config.cache_aware_compaction.model_dump() == {
            "enabled": False,
            "cache_ttl_seconds": 600,
            "max_cold_cache_catchup_passes": 4,
            "hot_cache_pressure_factor": 5.5,
            "hot_cache_budget_headroom_ratio": 0.25,
            "cold_cache_observation_threshold": 5,
            "critical_budget_pressure_ratio": DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO,
        }
        assert config.dynamic_leaf_chunk_tokens.model_dump() == {
            "enabled": True,
            "max": 60000,
        }

    def test_plugin_config_fills_gaps_when_env_absent(self) -> None:
        """Mirror of TS ``plugin config fills gaps when env vars are absent``."""
        env = {"LCM_CONTEXT_THRESHOLD": "0.9"}
        plugin_config = {
            "contextThreshold": 0.5,  # overridden by env
            "freshTailCount": 16,  # plugin wins (no env)
            "newSessionRetainDepth": 4,  # plugin wins (no env)
            "incrementalMaxDepth": -1,  # plugin wins (no env)
        }
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(env, plugin_config)
        assert config.context_threshold == 0.9  # env wins
        assert config.fresh_tail_count == 16  # plugin config
        assert config.new_session_retain_depth == 4  # plugin config
        assert config.incremental_max_depth == -1  # plugin config
        assert config.leaf_min_fanout == 8  # hardcoded default

    def test_env_database_path_overrides_plugin_config(self) -> None:
        """Mirror of TS ``env databasePath overrides plugin config``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {"LCM_DATABASE_PATH": "/env/path/lcm.db"},
                {"databasePath": "/plugin/path/lcm.db"},
            )
        assert config.database_path == "/env/path/lcm.db"

    def test_env_large_files_dir_overrides_plugin_config(self) -> None:
        """Mirror of TS ``env largeFilesDir overrides plugin config``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {"LCM_LARGE_FILES_DIR": "/env/path/lcm-files"},
                {"largeFilesDir": "/plugin/path/lcm-files"},
            )
        assert config.large_files_dir == "/env/path/lcm-files"

    def test_env_overrides_expansion_model_and_provider(self) -> None:
        """Mirror of TS ``env vars override expansionModel and expansionProvider``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {
                    "LCM_EXPANSION_MODEL": "anthropic/claude-sonnet-4-6",
                    "LCM_EXPANSION_PROVIDER": "openrouter",
                },
                {
                    "expansionModel": "anthropic/claude-haiku-4-5",
                    "expansionProvider": "anthropic",
                },
            )
        assert config.expansion_model == "anthropic/claude-sonnet-4-6"
        assert config.expansion_provider == "openrouter"

    def test_env_delegation_timeout_overrides_plugin_config(self) -> None:
        """Mirror of TS ``env var overrides delegationTimeoutMs``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {"LCM_DELEGATION_TIMEOUT_MS": "180000"},
                {"delegationTimeoutMs": 300000},
            )
        assert config.delegation_timeout_ms == 180000

    def test_falls_back_to_plugin_delegation_timeout_when_env_invalid(self) -> None:
        """Mirror of TS ``falls back to plugin delegationTimeoutMs when env value is invalid``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {"LCM_DELEGATION_TIMEOUT_MS": "not-a-number"},
                {"delegationTimeoutMs": 300000},
            )
        assert config.delegation_timeout_ms == 300000

    def test_summary_model_env_overrides(self) -> None:
        """Mirror of TS ``uses summary model overrides from env vars``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {
                    "LCM_SUMMARY_PROVIDER": "anthropic",
                    "LCM_SUMMARY_MODEL": "claude-3-5-haiku",
                },
                {},
            )
        assert config.summary_provider == "anthropic"
        assert config.summary_model == "claude-3-5-haiku"

    def test_summary_model_plugin_config_fills_when_env_absent(self) -> None:
        """Mirror of TS ``uses summary model overrides from plugin config when env vars are absent``."""
        config = resolve_lcm_config({}, {"summaryProvider": "openai", "summaryModel": "gpt-5-mini"})
        assert config.summary_provider == "openai"
        assert config.summary_model == "gpt-5-mini"

    def test_env_summary_overrides_beat_plugin_config(self) -> None:
        """Mirror of TS ``prefers env summary overrides over plugin config``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {
                    "LCM_SUMMARY_PROVIDER": "anthropic",
                    "LCM_SUMMARY_MODEL": "claude-3-5-haiku",
                },
                {"summaryProvider": "openai", "summaryModel": "gpt-5-mini"},
            )
        assert config.summary_provider == "anthropic"
        assert config.summary_model == "claude-3-5-haiku"

    def test_keeps_empty_ignore_session_patterns_out(self) -> None:
        """Mirror of TS ``keeps empty ignore session patterns out of resolved config``.

        Empty/whitespace-only comma-segments are dropped when parsing
        env-var pattern lists.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {"LCM_IGNORE_SESSION_PATTERNS": " agent:*:cron:* , , "},
                {"ignoreSessionPatterns": ["agent:*:test:*"]},
            )
        assert config.ignore_session_patterns == ["agent:*:cron:*"]

    def test_keeps_empty_stateless_session_patterns_out(self) -> None:
        """Mirror of TS ``keeps empty stateless session patterns out of resolved config``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {"LCM_STATELESS_SESSION_PATTERNS": " agent:*:ephemeral:** , , "},
                {"statelessSessionPatterns": ["agent:*:preview:*"]},
            )
        assert config.stateless_session_patterns == ["agent:*:ephemeral:**"]

    def test_env_max_assembly_token_budget_and_overage_factor(self) -> None:
        """Mirror of TS ``env vars override summaryMaxOverageFactor and maxAssemblyTokenBudget``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {
                    "LCM_SUMMARY_MAX_OVERAGE_FACTOR": "2.5",
                    "LCM_MAX_ASSEMBLY_TOKEN_BUDGET": "16000",
                },
                {"summaryMaxOverageFactor": 5, "maxAssemblyTokenBudget": 30000},
            )
        assert config.summary_max_overage_factor == 2.5
        assert config.max_assembly_token_budget == 16000


class TestDerivedDefaults:
    """Defaults that depend on other resolved fields."""

    def test_bootstrap_max_tokens_derives_from_leaf_chunk_tokens(self) -> None:
        """Mirror of TS ``derives bootstrapMaxTokens from leafChunkTokens and allows override``."""
        # 80_000 * 0.3 = 24_000 > 6000 ⇒ 24_000.
        assert resolve_lcm_config({}, {"leafChunkTokens": 80_000}).bootstrap_max_tokens == 24_000
        # Explicit override wins over derived.
        assert (
            resolve_lcm_config(
                {}, {"leafChunkTokens": 80_000, "bootstrapMaxTokens": 12_345}
            ).bootstrap_max_tokens
            == 12_345
        )

    def test_bootstrap_max_tokens_floor_is_6000(self) -> None:
        """Verify the ``max(6000, leaf_chunk_tokens * 0.3)`` floor.

        leaf_chunk_tokens=10000 ⇒ 10000*0.3 = 3000, floor to 6000.
        """
        config = resolve_lcm_config({}, {"leafChunkTokens": 10_000})
        assert config.bootstrap_max_tokens == 6000

    def test_env_overrides_bootstrap_max_tokens(self) -> None:
        """Mirror of TS ``env vars override bootstrapMaxTokens``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {"LCM_BOOTSTRAP_MAX_TOKENS": "4321"},
                {"bootstrapMaxTokens": 12_345},
            )
        assert config.bootstrap_max_tokens == 4321

    def test_invalid_numeric_envs_fall_back(self) -> None:
        """Mirror of TS ``falls back cleanly when numeric env vars are invalid``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {
                    "LCM_LEAF_CHUNK_TOKENS": "oops",
                    "LCM_BOOTSTRAP_MAX_TOKENS": "still-nope",
                    "LCM_CONTEXT_THRESHOLD": "bad",
                    "LCM_SUMMARY_MAX_OVERAGE_FACTOR": "nah",
                },
                {
                    "leafChunkTokens": 80_000,
                    "contextThreshold": 0.5,
                    "summaryMaxOverageFactor": 5,
                },
            )
        assert config.leaf_chunk_tokens == 80_000
        assert config.bootstrap_max_tokens == 24_000
        assert config.context_threshold == 0.5
        assert config.summary_max_overage_factor == 5.0

    def test_dynamic_leaf_chunk_max_defaults_to_2x_static(self) -> None:
        """Mirror of TS ``defaults dynamic leaf chunk token max to 2x the static floor``."""
        config = resolve_lcm_config({}, {"leafChunkTokens": 24_000})
        assert config.dynamic_leaf_chunk_tokens.model_dump() == {
            "enabled": True,
            "max": 48_000,
        }

    def test_dynamic_leaf_chunk_max_clamps_to_static_floor(self) -> None:
        """Mirror of TS ``clamps dynamic leaf chunk token max so it never drops below the static floor``."""
        config = resolve_lcm_config(
            {},
            {
                "leafChunkTokens": 24_000,
                "dynamicLeafChunkTokens": {"enabled": True, "max": 12_000},
            },
        )
        assert config.dynamic_leaf_chunk_tokens.model_dump() == {
            "enabled": True,
            "max": 24_000,
        }

    def test_dynamic_leaf_chunk_tokens_from_plugin_config(self) -> None:
        """Mirror of TS ``reads dynamic leaf chunk token settings from plugin config``."""
        config = resolve_lcm_config(
            {},
            {
                "leafChunkTokens": 24_000,
                "dynamicLeafChunkTokens": {"enabled": True, "max": 42_000},
            },
        )
        assert config.dynamic_leaf_chunk_tokens.model_dump() == {
            "enabled": True,
            "max": 42_000,
        }


class TestClamps:
    """Validate the clamps documented in tests-and-config.md lines 295-298."""

    def test_hot_cache_budget_headroom_clamps_to_0_95(self) -> None:
        """``hot_cache_budget_headroom_ratio`` clamped to ``[0, 0.95]``."""
        config = resolve_lcm_config(
            {}, {"cacheAwareCompaction": {"hotCacheBudgetHeadroomRatio": 5.0}}
        )
        assert config.cache_aware_compaction.hot_cache_budget_headroom_ratio == 0.95

    def test_hot_cache_budget_headroom_clamps_to_0(self) -> None:
        """``hot_cache_budget_headroom_ratio`` clamped to min 0."""
        config = resolve_lcm_config(
            {}, {"cacheAwareCompaction": {"hotCacheBudgetHeadroomRatio": -1.0}}
        )
        assert config.cache_aware_compaction.hot_cache_budget_headroom_ratio == 0.0

    def test_critical_budget_pressure_ratio_clamps_to_1(self) -> None:
        """``critical_budget_pressure_ratio`` clamped to ``[0, 1]``."""
        config = resolve_lcm_config(
            {}, {"cacheAwareCompaction": {"criticalBudgetPressureRatio": 99.0}}
        )
        assert config.cache_aware_compaction.critical_budget_pressure_ratio == 1.0

    def test_critical_budget_pressure_ratio_clamps_to_0(self) -> None:
        """``critical_budget_pressure_ratio`` clamped to min 0."""
        config = resolve_lcm_config(
            {}, {"cacheAwareCompaction": {"criticalBudgetPressureRatio": -0.5}}
        )
        assert config.cache_aware_compaction.critical_budget_pressure_ratio == 0.0

    def test_hot_cache_pressure_factor_clamps_to_min_1(self) -> None:
        """``hot_cache_pressure_factor`` clamped to min 1."""
        config = resolve_lcm_config({}, {"cacheAwareCompaction": {"hotCachePressureFactor": 0.5}})
        assert config.cache_aware_compaction.hot_cache_pressure_factor == 1.0


# ===========================================================================
# 2. resolve_lcm_config_with_diagnostics — pattern-array provenance
# ===========================================================================


class TestDiagnostics:
    """Pattern-array source tracking + env-override flags."""

    def test_reports_session_pattern_sources_with_env_override_flags(self) -> None:
        """Mirror of TS ``reports session pattern sources and env override diagnostics``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config, diagnostics = resolve_lcm_config_with_diagnostics(
                {
                    "LCM_IGNORE_SESSION_PATTERNS": "agent:*:cron:*, agent:main:subagent:**",
                    "LCM_STATELESS_SESSION_PATTERNS": "agent:*:ephemeral:**",
                },
                {
                    "ignoreSessionPatterns": ["agent:*:test:*"],
                    "statelessSessionPatterns": ["agent:*:preview:*"],
                },
            )
        assert config.ignore_session_patterns == [
            "agent:*:cron:*",
            "agent:main:subagent:**",
        ]
        assert config.stateless_session_patterns == ["agent:*:ephemeral:**"]
        assert diagnostics.model_dump() == {
            "ignore_session_patterns_source": "env",
            "stateless_session_patterns_source": "env",
            "ignore_session_patterns_env_overrides_config": True,
            "stateless_session_patterns_env_overrides_config": True,
        }

    def test_diagnostics_with_only_plugin_config(self) -> None:
        """Plugin-config patterns: source=plugin-config, no env override."""
        config, diagnostics = resolve_lcm_config_with_diagnostics(
            {},
            {
                "ignoreSessionPatterns": ["agent:*:test:*"],
                "statelessSessionPatterns": ["agent:*:preview:*"],
            },
        )
        assert config.ignore_session_patterns == ["agent:*:test:*"]
        assert diagnostics.ignore_session_patterns_source == "plugin-config"
        assert diagnostics.stateless_session_patterns_source == "plugin-config"
        assert diagnostics.ignore_session_patterns_env_overrides_config is False
        assert diagnostics.stateless_session_patterns_env_overrides_config is False

    def test_diagnostics_with_no_input_uses_defaults(self) -> None:
        """Neither env nor plugin-config: source=default."""
        config, diagnostics = resolve_lcm_config_with_diagnostics({}, {})
        assert config.ignore_session_patterns == []
        assert diagnostics.ignore_session_patterns_source == "default"
        assert diagnostics.stateless_session_patterns_source == "default"

    def test_describe_lcm_config_source(self) -> None:
        """``describe_lcm_config_source`` returns human-readable labels."""
        assert describe_lcm_config_source("env") == "env"
        assert describe_lcm_config_source("plugin-config") == "plugin config"
        assert describe_lcm_config_source("default") == "defaults"


# ===========================================================================
# 3. State-dir resolvers
# ===========================================================================


class TestResolveOpenclawStateDir:
    """Mirror of TS ``describe("resolveOpenclawStateDir", ...)``."""

    def test_falls_back_to_dot_openclaw_when_unset(self) -> None:
        """Mirror of TS ``falls back to ~/.openclaw when OPENCLAW_STATE_DIR is unset``."""
        result = resolve_openclaw_state_dir({})
        assert result == str(Path.home() / ".openclaw")

    def test_returns_explicit_value(self) -> None:
        """Mirror of TS ``returns OPENCLAW_STATE_DIR when set``."""
        result = resolve_openclaw_state_dir({"OPENCLAW_STATE_DIR": "/custom/state"})
        assert result == "/custom/state"

    def test_trims_whitespace(self) -> None:
        """Mirror of TS ``trims whitespace from OPENCLAW_STATE_DIR``."""
        result = resolve_openclaw_state_dir({"OPENCLAW_STATE_DIR": "  /custom/state  "})
        assert result == "/custom/state"

    def test_empty_string_falls_back(self) -> None:
        """Mirror of TS ``falls back to ~/.openclaw when OPENCLAW_STATE_DIR is an empty string``."""
        result = resolve_openclaw_state_dir({"OPENCLAW_STATE_DIR": ""})
        assert result == str(Path.home() / ".openclaw")

    def test_whitespace_only_falls_back(self) -> None:
        """Mirror of TS ``falls back to ~/.openclaw when OPENCLAW_STATE_DIR is whitespace only``."""
        result = resolve_openclaw_state_dir({"OPENCLAW_STATE_DIR": "   "})
        assert result == str(Path.home() / ".openclaw")


class TestResolveHermesStateDir:
    """Hermes-side analog (Python-specific addition)."""

    def test_falls_back_to_dot_hermes_when_unset(self) -> None:
        result = resolve_hermes_state_dir({})
        assert result == str(Path.home() / ".hermes")

    def test_returns_explicit_hermes_home(self) -> None:
        result = resolve_hermes_state_dir({"HERMES_HOME": "/explicit/hermes"})
        assert result == "/explicit/hermes"

    def test_trims_whitespace(self) -> None:
        result = resolve_hermes_state_dir({"HERMES_HOME": "  /trim/me  "})
        assert result == "/trim/me"


class TestLargeFilesDir:
    """Mirror of TS ``describe("resolveLcmConfig largeFilesDir", ...)``."""

    def test_default_uses_openclaw_state_dir(self) -> None:
        """Mirror of TS ``defaults largeFilesDir to ~/.openclaw/lcm-files when OPENCLAW_STATE_DIR is unset``."""
        config = resolve_lcm_config({}, {})
        assert config.large_files_dir == str(Path.home() / ".openclaw" / "lcm-files")

    def test_uses_openclaw_state_dir_when_set(self) -> None:
        """Mirror of TS ``uses OPENCLAW_STATE_DIR for largeFilesDir when set``."""
        config = resolve_lcm_config({"OPENCLAW_STATE_DIR": "/custom/state"}, {})
        assert config.large_files_dir == "/custom/state/lcm-files"

    def test_env_overrides_openclaw_state_dir(self) -> None:
        """Mirror of TS ``LCM_LARGE_FILES_DIR env var overrides OPENCLAW_STATE_DIR for largeFilesDir``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {
                    "OPENCLAW_STATE_DIR": "/custom/state",
                    "LCM_LARGE_FILES_DIR": "/explicit/files",
                },
                {},
            )
        assert config.large_files_dir == "/explicit/files"

    def test_plugin_config_overrides_openclaw_state_dir(self) -> None:
        """Mirror of TS ``largeFilesDir plugin config overrides OPENCLAW_STATE_DIR``."""
        config = resolve_lcm_config(
            {"OPENCLAW_STATE_DIR": "/custom/state"},
            {"largeFilesDir": "/plugin/files"},
        )
        assert config.large_files_dir == "/plugin/files"


class TestDatabasePath:
    """Mirror of TS ``describe("resolveLcmConfig databasePath uses OPENCLAW_STATE_DIR", ...)``."""

    def test_uses_openclaw_state_dir_for_default(self) -> None:
        """Mirror of TS ``uses OPENCLAW_STATE_DIR for default databasePath``."""
        config = resolve_lcm_config({"OPENCLAW_STATE_DIR": "/custom/state"}, {})
        assert config.database_path == "/custom/state/lcm.db"

    def test_env_overrides_openclaw_state_dir(self) -> None:
        """Mirror of TS ``LCM_DATABASE_PATH still overrides OPENCLAW_STATE_DIR``."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            config = resolve_lcm_config(
                {
                    "OPENCLAW_STATE_DIR": "/custom/state",
                    "LCM_DATABASE_PATH": "/explicit/db.sqlite",
                },
                {},
            )
        assert config.database_path == "/explicit/db.sqlite"


# ===========================================================================
# 4. resolve_voyage_api_key — three-tier resolver per ADR-022
# ===========================================================================


class TestResolveVoyageApiKey:
    """ADR-022 three-tier: config inline > env > $HERMES_HOME file."""

    def test_inline_config_wins(self, tmp_path: Path) -> None:
        """Tier 1: config.voyage_api_key is non-empty after strip."""
        config = LcmConfig(voyage_api_key="config-key")
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "env-key"},
            hermes_home=tmp_path,
        )
        assert result == "config-key"

    def test_env_wins_when_config_empty(self, tmp_path: Path) -> None:
        """Tier 2: env wins when config tier is empty."""
        config = LcmConfig(voyage_api_key="")
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "env-key"},
            hermes_home=tmp_path,
        )
        assert result == "env-key"

    def test_env_wins_when_config_none(self, tmp_path: Path) -> None:
        """Tier 2: env wins when config.voyage_api_key is None."""
        config = LcmConfig()
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "env-key"},
            hermes_home=tmp_path,
        )
        assert result == "env-key"

    def test_file_wins_when_config_and_env_empty(self, tmp_path: Path) -> None:
        """Tier 3: file wins when config and env are both empty."""
        cred_dir = tmp_path / "lossless-hermes" / "credentials"
        cred_dir.mkdir(parents=True)
        cred_file = cred_dir / "voyage-api-key"
        cred_file.write_text("file-key\n", encoding="utf-8")  # whitespace stripped
        config = LcmConfig()
        result = resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        assert result == "file-key"

    def test_returns_none_when_all_tiers_empty(self, tmp_path: Path) -> None:
        """All tiers empty ⇒ None (caller decides how to error)."""
        config = LcmConfig()
        result = resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        assert result is None

    def test_whitespace_only_env_falls_through(self, tmp_path: Path) -> None:
        """Whitespace-only env counts as empty; falls through to file tier."""
        cred_dir = tmp_path / "lossless-hermes" / "credentials"
        cred_dir.mkdir(parents=True)
        (cred_dir / "voyage-api-key").write_text("file-fallback", encoding="utf-8")
        config = LcmConfig()
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "   "},
            hermes_home=tmp_path,
        )
        assert result == "file-fallback"

    def test_whitespace_only_file_falls_through_to_none(self, tmp_path: Path) -> None:
        """Whitespace-only file counts as empty; returns None."""
        cred_dir = tmp_path / "lossless-hermes" / "credentials"
        cred_dir.mkdir(parents=True)
        (cred_dir / "voyage-api-key").write_text("   \n  ", encoding="utf-8")
        config = LcmConfig()
        result = resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        assert result is None

    def test_uses_default_hermes_home_when_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defaults ``hermes_home`` from the passed env (HERMES_HOME → tmp_path)."""
        cred_dir = tmp_path / "lossless-hermes" / "credentials"
        cred_dir.mkdir(parents=True)
        (cred_dir / "voyage-api-key").write_text("via-default-home", encoding="utf-8")
        config = LcmConfig()
        # Pass env with HERMES_HOME — function should derive hermes_home from
        # the env mapping it receives. (No hermes_home= override.)
        result = resolve_voyage_api_key(config, env={"HERMES_HOME": str(tmp_path)})
        assert result == "via-default-home"


# ===========================================================================
# 5. LcmConfig model validation
# ===========================================================================


class TestLcmConfigValidation:
    """Pydantic validation behavior: extra=forbid + ge/le bounds."""

    def test_rejects_unknown_field(self) -> None:
        """``extra='forbid'`` — typo at construction time fails loud."""
        with pytest.raises(ValidationError) as exc:
            LcmConfig(threshhold=0.7)  # type: ignore[call-arg]
        assert "threshhold" in str(exc.value)

    def test_rejects_negative_threshold(self) -> None:
        """``context_threshold`` constrained to ``[0, 1]``."""
        with pytest.raises(ValidationError) as exc:
            LcmConfig(context_threshold=-0.1)
        assert "context_threshold" in str(exc.value)

    def test_rejects_threshold_above_1(self) -> None:
        """``context_threshold`` ≤ 1."""
        with pytest.raises(ValidationError) as exc:
            LcmConfig(context_threshold=1.5)
        assert "context_threshold" in str(exc.value)

    def test_rejects_negative_fresh_tail_count(self) -> None:
        """``fresh_tail_count`` ≥ 1."""
        with pytest.raises(ValidationError):
            LcmConfig(fresh_tail_count=0)

    def test_rejects_bad_proactive_mode(self) -> None:
        """``proactive_threshold_compaction_mode`` enum."""
        with pytest.raises(ValidationError):
            LcmConfig(proactive_threshold_compaction_mode="badmode")  # type: ignore[arg-type]

    def test_rejects_bad_auto_rotate_startup(self) -> None:
        """``auto_rotate_session_files.startup`` enum."""
        from lossless_hermes.db.config import AutoRotateSessionFilesConfig

        with pytest.raises(ValidationError):
            AutoRotateSessionFilesConfig(startup="trash")  # type: ignore[arg-type]

    def test_rejects_too_low_large_file_threshold(self) -> None:
        """``large_file_token_threshold`` ≥ 1000."""
        with pytest.raises(ValidationError):
            LcmConfig(large_file_token_threshold=500)

    def test_rejects_negative_new_session_retain_depth(self) -> None:
        """``new_session_retain_depth`` ≥ -1 (matches TS allowing -1 for "keep all")."""
        with pytest.raises(ValidationError):
            LcmConfig(new_session_retain_depth=-2)
        # -1 is OK ("keep all").
        config = LcmConfig(new_session_retain_depth=-1)
        assert config.new_session_retain_depth == -1


# ===========================================================================
# 5b. embeddings_enabled — ADR-033 opt-in flag (Hermes-only, no TS mirror)
# ===========================================================================


class TestEmbeddingsEnabledFlag:
    """``embeddings_enabled`` — the ADR-033 (issue #133) opt-in flag.

    Per ADR-033, ``lcm_grep``'s ``hybrid`` / ``semantic`` retrieval modes
    are opt-in and **OFF by default**. This flag is the config knob; it has
    **no TS equivalent** (so it is intentionally absent from
    ``_TS_PARITY_TABLE``). Resolution follows the strict-``"true"`` boolean
    convention of ``prompt_aware_eviction`` / ``agent_compaction_tool_enabled``
    — env var > plugin config > hardcoded ``False``.
    """

    def test_default_is_false(self) -> None:
        """ADR-033 core: with no env / plugin config, embeddings are OFF.

        This is the load-bearing default — a keyless install gets the
        FTS-first posture, not the hybrid-primary one.
        """
        config = resolve_lcm_config({}, {})
        assert config.embeddings_enabled is False

    def test_model_default_is_false(self) -> None:
        """The pydantic model default itself (not just the resolver) is False."""
        assert LcmConfig().embeddings_enabled is False

    def test_plugin_config_snake_case_enables(self) -> None:
        """``embeddings_enabled: true`` under ``lossless_hermes:`` opts in."""
        config = resolve_lcm_config({}, {"embeddings_enabled": True})
        assert config.embeddings_enabled is True

    def test_plugin_config_camel_case_enables(self) -> None:
        """The camelCase alias ``embeddingsEnabled`` is also accepted."""
        config = resolve_lcm_config({}, {"embeddingsEnabled": True})
        assert config.embeddings_enabled is True

    def test_hermes_env_var_enables(self) -> None:
        """``HERMES_EMBEDDINGS_ENABLED=true`` opts in; no warning fires."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning fails this test
            config = resolve_lcm_config({"HERMES_EMBEDDINGS_ENABLED": "true"}, {})
        assert config.embeddings_enabled is True

    def test_lcm_env_var_enables_with_deprecation_warning(self) -> None:
        """The legacy ``LCM_EMBEDDINGS_ENABLED`` alias works but warns."""
        with pytest.warns(
            DeprecationWarning,
            match="LCM_EMBEDDINGS_ENABLED.*HERMES_EMBEDDINGS_ENABLED",
        ):
            config = resolve_lcm_config({"LCM_EMBEDDINGS_ENABLED": "true"}, {})
        assert config.embeddings_enabled is True

    def test_env_strict_true_semantics(self) -> None:
        """Only the exact string ``"true"`` enables — like the other opt-in
        flags (``prompt_aware_eviction``). ``"1"`` / ``"yes"`` / ``"True"``
        do NOT enable via env (strict, not the ``!= "false"`` variant)."""
        for non_true in ("false", "1", "yes", "TRUE", "True", "", "on"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                config = resolve_lcm_config({"HERMES_EMBEDDINGS_ENABLED": non_true}, {})
            assert config.embeddings_enabled is False, (
                f"env value {non_true!r} should NOT enable embeddings"
            )

    def test_env_overrides_plugin_config(self) -> None:
        """Env var beats plugin config (standard precedence) — env can both
        force-on and force-off relative to a plugin-config value."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            # Plugin config says on, env says off → env wins.
            off = resolve_lcm_config(
                {"HERMES_EMBEDDINGS_ENABLED": "false"},
                {"embeddings_enabled": True},
            )
            # Plugin config says off, env says on → env wins.
            on = resolve_lcm_config(
                {"HERMES_EMBEDDINGS_ENABLED": "true"},
                {"embeddings_enabled": False},
            )
        assert off.embeddings_enabled is False
        assert on.embeddings_enabled is True

    def test_voyage_key_alone_does_not_enable(self) -> None:
        """ADR-033 §Open-Q2 "both-required": a Voyage key WITHOUT the flag
        does not silently enable embeddings.

        An operator who set ``VOYAGE_API_KEY`` (or the inline
        ``voyage_api_key`` config) for another purpose must still flip
        ``embeddings_enabled`` explicitly — the key is not an implicit
        opt-in.
        """
        config = resolve_lcm_config(
            {"VOYAGE_API_KEY": "vk-present"},
            {"voyage_api_key": "vk-also-present"},
        )
        assert config.embeddings_enabled is False

    def test_recognized_plugin_config_key(self) -> None:
        """``embeddings_enabled`` is in the recognized-keys allowlist — it
        does NOT trip the ``extra='forbid'`` unknown-key rejection."""
        # Would raise ValidationError if the key were unrecognized.
        config = resolve_lcm_config({}, {"embeddings_enabled": False})
        assert config.embeddings_enabled is False

    def test_invalid_plugin_config_value_falls_to_default(self) -> None:
        """A non-bool-coercible plugin value falls through to the False
        default rather than crashing the resolver (matches the resolver's
        tolerance for other booleans)."""
        config = resolve_lcm_config({}, {"embeddings_enabled": "not-a-bool"})
        assert config.embeddings_enabled is False


# ===========================================================================
# 6. Env-var alias deprecation warnings (Phase 1 policy)
# ===========================================================================


class TestDeprecationWarnings:
    """``LCM_*`` aliases must emit one ``DeprecationWarning`` per env var per
    process (debounced via lru_cache)."""

    def test_lcm_alias_emits_deprecation_warning(self) -> None:
        """A single ``LCM_FOO`` read emits one ``DeprecationWarning``."""
        with pytest.warns(DeprecationWarning, match="LCM_CONTEXT_THRESHOLD"):
            resolve_lcm_config({"LCM_CONTEXT_THRESHOLD": "0.8"}, {})

    def test_hermes_alias_does_not_warn(self) -> None:
        """Reading ``HERMES_FOO`` does not emit a warning."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning would fail this test
            config = resolve_lcm_config({"HERMES_CONTEXT_THRESHOLD": "0.8"}, {})
            assert config.context_threshold == 0.8

    def test_lcm_alias_debounced(self) -> None:
        """Multiple resolves with the same ``LCM_*`` env var emit ONE warning."""
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", DeprecationWarning)
            resolve_lcm_config({"LCM_CONTEXT_THRESHOLD": "0.8"}, {})
            resolve_lcm_config({"LCM_CONTEXT_THRESHOLD": "0.9"}, {})
            resolve_lcm_config({"LCM_CONTEXT_THRESHOLD": "0.95"}, {})
        # Only one DeprecationWarning for LCM_CONTEXT_THRESHOLD.
        ctx_warns = [
            w
            for w in captured
            if "LCM_CONTEXT_THRESHOLD" in str(w.message) and w.category is DeprecationWarning
        ]
        assert len(ctx_warns) == 1

    def test_hermes_prefix_wins_over_lcm(self) -> None:
        """When both prefixes set, ``HERMES_*`` wins and no warning fires."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # would fail on any warning
            config = resolve_lcm_config(
                {
                    "HERMES_CONTEXT_THRESHOLD": "0.9",
                    "LCM_CONTEXT_THRESHOLD": "0.5",
                },
                {},
            )
            assert config.context_threshold == 0.9

    @pytest.mark.parametrize(
        "lcm_var,hermes_var,value",
        [
            ("LCM_LEAF_CHUNK_TOKENS", "HERMES_LEAF_CHUNK_TOKENS", "12345"),
            ("LCM_SUMMARY_PROVIDER", "HERMES_SUMMARY_PROVIDER", "anthropic"),
            ("LCM_DATABASE_PATH", "HERMES_DATABASE_PATH", "/tmp/lcm.db"),
            ("LCM_AGENT_COMPACTION_TOOL_ENABLED", "HERMES_AGENT_COMPACTION_TOOL_ENABLED", "true"),
            ("LCM_CIRCUIT_BREAKER_THRESHOLD", "HERMES_CIRCUIT_BREAKER_THRESHOLD", "10"),
        ],
    )
    def test_lcm_aliases_per_var(self, lcm_var: str, hermes_var: str, value: str) -> None:
        """Every common ``LCM_*`` env var emits its own warning citing the
        matching ``HERMES_*`` replacement."""
        with pytest.warns(DeprecationWarning, match=f"{lcm_var}.*{hermes_var}"):
            resolve_lcm_config({lcm_var: value}, {})


# ===========================================================================
# 7. Table-driven default parity (snake_case ↔ TS camelCase)
# ===========================================================================


# Mapping of TS camelCase field → (Python snake_case attribute path, expected default)
# Used by the parity test below; locks every TS default to its Python mirror.
_TS_PARITY_TABLE: list[tuple[str, str, Any]] = [
    ("enabled", "enabled", True),
    ("ignoreSessionPatterns", "ignore_session_patterns", []),
    ("statelessSessionPatterns", "stateless_session_patterns", []),
    ("skipStatelessSessions", "skip_stateless_sessions", True),
    ("contextThreshold", "context_threshold", 0.75),
    ("freshTailCount", "fresh_tail_count", 64),
    ("freshTailMaxTokens", "fresh_tail_max_tokens", None),
    ("promptAwareEviction", "prompt_aware_eviction", False),
    ("newSessionRetainDepth", "new_session_retain_depth", 2),
    ("leafMinFanout", "leaf_min_fanout", 8),
    ("condensedMinFanout", "condensed_min_fanout", 4),
    ("condensedMinFanoutHard", "condensed_min_fanout_hard", 2),
    ("incrementalMaxDepth", "incremental_max_depth", 1),
    ("leafChunkTokens", "leaf_chunk_tokens", 20000),
    ("bootstrapMaxTokens", "bootstrap_max_tokens", 6000),
    ("leafTargetTokens", "leaf_target_tokens", 4000),
    ("condensedTargetTokens", "condensed_target_tokens", 2000),
    ("maxExpandTokens", "max_expand_tokens", 4000),
    ("largeFileTokenThreshold", "large_file_token_threshold", 25000),
    ("summaryProvider", "summary_provider", ""),
    ("summaryModel", "summary_model", ""),
    ("largeFileSummaryProvider", "large_file_summary_provider", ""),
    ("largeFileSummaryModel", "large_file_summary_model", ""),
    ("expansionProvider", "expansion_provider", ""),
    ("expansionModel", "expansion_model", ""),
    ("delegationTimeoutMs", "delegation_timeout_ms", 120000),
    ("summaryTimeoutMs", "summary_timeout_ms", 60000),
    ("pruneHeartbeatOk", "prune_heartbeat_ok", False),
    ("transcriptGcEnabled", "transcript_gc_enabled", False),
    ("agentCompactionToolEnabled", "agent_compaction_tool_enabled", False),
    ("proactiveThresholdCompactionMode", "proactive_threshold_compaction_mode", "deferred"),
    ("maxAssemblyTokenBudget", "max_assembly_token_budget", None),
    ("toolResultTokenBudget", "tool_result_token_budget", None),
    ("summaryMaxOverageFactor", "summary_max_overage_factor", 3.0),
    ("customInstructions", "custom_instructions", ""),
    ("circuitBreakerThreshold", "circuit_breaker_threshold", 5),
    ("circuitBreakerCooldownMs", "circuit_breaker_cooldown_ms", 1_800_000),
    ("fallbackProviders", "fallback_providers", []),
    # Nested:
    ("autoRotateSessionFiles.enabled", "auto_rotate_session_files.enabled", True),
    (
        "autoRotateSessionFiles.sizeBytes",
        "auto_rotate_session_files.size_bytes",
        DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES,
    ),
    ("autoRotateSessionFiles.startup", "auto_rotate_session_files.startup", "rotate"),
    ("autoRotateSessionFiles.runtime", "auto_rotate_session_files.runtime", "rotate"),
    ("cacheAwareCompaction.enabled", "cache_aware_compaction.enabled", True),
    ("cacheAwareCompaction.cacheTTLSeconds", "cache_aware_compaction.cache_ttl_seconds", 300),
    (
        "cacheAwareCompaction.maxColdCacheCatchupPasses",
        "cache_aware_compaction.max_cold_cache_catchup_passes",
        2,
    ),
    (
        "cacheAwareCompaction.hotCachePressureFactor",
        "cache_aware_compaction.hot_cache_pressure_factor",
        4.0,
    ),
    (
        "cacheAwareCompaction.hotCacheBudgetHeadroomRatio",
        "cache_aware_compaction.hot_cache_budget_headroom_ratio",
        0.2,
    ),
    (
        "cacheAwareCompaction.coldCacheObservationThreshold",
        "cache_aware_compaction.cold_cache_observation_threshold",
        3,
    ),
    (
        "cacheAwareCompaction.criticalBudgetPressureRatio",
        "cache_aware_compaction.critical_budget_pressure_ratio",
        DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO,
    ),
    ("dynamicLeafChunkTokens.enabled", "dynamic_leaf_chunk_tokens.enabled", True),
    ("dynamicLeafChunkTokens.max", "dynamic_leaf_chunk_tokens.max", 40000),
]


@pytest.mark.parametrize("ts_field,py_field,expected_default", _TS_PARITY_TABLE)
def test_default_parity_snake_case_to_ts(
    ts_field: str, py_field: str, expected_default: Any
) -> None:
    """Every TS default must mirror in the Python snake_case field.

    Drives off the ``_TS_PARITY_TABLE`` above (one row per field).
    Failure means a TS default drifted from its Python mirror, or
    a new field landed without its expected default.
    """
    config = resolve_lcm_config({}, {})
    # Walk nested attribute path.
    obj: Any = config
    for segment in py_field.split("."):
        obj = getattr(obj, segment)
    assert obj == expected_default, (
        f"{ts_field} → {py_field}: expected {expected_default!r}, got {obj!r}"
    )


# ===========================================================================
# 8. WorkerConfig (kept for back-compat)
# ===========================================================================


def test_worker_config_instantiates_empty() -> None:
    """``WorkerConfig`` is still empty pending Epic 02."""
    from lossless_hermes.db.config import WorkerConfig

    wc = WorkerConfig()
    assert wc.model_dump() == {}


def test_worker_config_rejects_unknown_field() -> None:
    """``WorkerConfig`` still has ``extra='forbid'``."""
    from lossless_hermes.db.config import WorkerConfig

    with pytest.raises(ValidationError):
        WorkerConfig(interval_s=60)  # type: ignore[call-arg]


# ===========================================================================
# 9. Diagnostics return-type shape
# ===========================================================================


def test_resolve_with_diagnostics_returns_tuple() -> None:
    """Return type is a 2-tuple ``(LcmConfig, LcmConfigDiagnostics)``."""
    result = resolve_lcm_config_with_diagnostics({}, {})
    assert isinstance(result, tuple)
    assert len(result) == 2
    config, diagnostics = result
    assert isinstance(config, LcmConfig)
    assert isinstance(diagnostics, LcmConfigDiagnostics)
