---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port db/config.ts → db/config.py'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/db/config.ts`
- Lines: ~629 LOC
- Function(s)/class(es): `LcmConfig` shape, `resolveLcmConfig(env, pluginConfig) -> LcmConfig`, `resolveLcmConfigWithDiagnostics(...)` returning `[LcmConfig, LcmConfigDiagnostics]`, `resolveOpenclawStateDir(env)`.

## Target (Python)

- File: `src/lossless_hermes/db/config.py`
- Estimated LOC: ~700

## What this issue covers

The pure-function config resolver — env vars + plugin-config dict + defaults → typed `LcmConfig`. Per ADR-024 the file lives at `src/lossless_hermes/db/config.py` (matches TS path 1:1).

**Use `pydantic-settings`** (per `docs/porting-guides/tests-and-config.md` line 448): `pydantic.Field(default=...)` mirrors the TS `??` fallback exactly; `model_config = ConfigDict(extra="forbid")` mirrors `additionalProperties: false`.

### Env-var coverage

The full TS surface is **67 env-var overrides** (`LCM_*` prefixes), split across three groups in `docs/porting-guides/tests-and-config.md` lines 246–321:

1. **Core LcmConfig** (~52 vars) — `LCM_ENABLED`, `LCM_DATABASE_PATH`, `LCM_LARGE_FILES_DIR`, `LCM_IGNORE_SESSION_PATTERNS`, `LCM_STATELESS_SESSION_PATTERNS`, `LCM_SKIP_STATELESS_SESSIONS`, `LCM_CONTEXT_THRESHOLD`, `LCM_FRESH_TAIL_COUNT`, `LCM_FRESH_TAIL_MAX_TOKENS`, `LCM_PROMPT_AWARE_EVICTION_ENABLED`, `LCM_NEW_SESSION_RETAIN_DEPTH`, `LCM_LEAF_MIN_FANOUT`, `LCM_CONDENSED_MIN_FANOUT`, `LCM_CONDENSED_MIN_FANOUT_HARD`, `LCM_INCREMENTAL_MAX_DEPTH`, `LCM_LEAF_CHUNK_TOKENS`, `LCM_BOOTSTRAP_MAX_TOKENS`, `LCM_LEAF_TARGET_TOKENS`, `LCM_CONDENSED_TARGET_TOKENS`, `LCM_MAX_EXPAND_TOKENS`, `LCM_LARGE_FILE_TOKEN_THRESHOLD`, `LCM_SUMMARY_PROVIDER`, `LCM_SUMMARY_MODEL`, `LCM_LARGE_FILE_SUMMARY_PROVIDER`, `LCM_LARGE_FILE_SUMMARY_MODEL`, `LCM_EXPANSION_PROVIDER`, `LCM_EXPANSION_MODEL`, `LCM_DELEGATION_TIMEOUT_MS`, `LCM_SUMMARY_TIMEOUT_MS`, `LCM_PRUNE_HEARTBEAT_OK`, `LCM_TRANSCRIPT_GC_ENABLED` (**drops on Hermes** — transcript GC is openclaw-specific), `LCM_AGENT_COMPACTION_TOOL_ENABLED`, `LCM_PROACTIVE_THRESHOLD_COMPACTION_MODE`, `LCM_AUTO_ROTATE_SESSION_FILES_*` (4 vars), `LCM_MAX_ASSEMBLY_TOKEN_BUDGET`, `LCM_TOOL_RESULT_TOKEN_BUDGET`, `LCM_SUMMARY_MAX_OVERAGE_FACTOR`, `LCM_CUSTOM_INSTRUCTIONS`, `LCM_CIRCUIT_BREAKER_THRESHOLD`, `LCM_CIRCUIT_BREAKER_COOLDOWN_MS`, `LCM_FALLBACK_PROVIDERS`, `LCM_CACHE_AWARE_COMPACTION_*` (8 vars), `LCM_DYNAMIC_LEAF_CHUNK_TOKENS_*` (2 vars).

2. **Embeddings / extraction-only** (6 vars consumed directly, not in `LcmConfig`) — `LCM_DEFAULT_TOKEN_BUDGET`, `LCM_SQLITE_VEC_PATH`, `LCM_DISABLE_SEMANTIC`, `LCM_EMBEDDING_DIM`, `LCM_EMBEDDING_MODEL`, `LCM_EXTRACTION_LLM_ENABLED`. **Out of scope for this issue** (Epic 05 owns these) — but document them in the config-module docstring so the next porter knows the full surface.

3. **Test-only env vars** — `LCM_TEST_VEC0_PATH`, `REAL_HOME`, `HOME`, `ANTHROPIC_API_KEY`. **Out of scope** — handled in `tests/conftest.py`.

4. **Hermes-side rename policy** (per `tests-and-config.md` line 556) — Phase 1 keeps `LCM_*` env vars accepted as aliases (emit `DeprecationWarning`); Phase 2 promotes `HERMES_*` as primary; Phase 3 drops `LCM_*`. For this issue: implement both prefixes via `pydantic-settings` `validation_alias` per field; emit one deprecation warning per `LCM_*` hit per process via `functools.lru_cache(maxsize=None)` (debounce the warning, not the value).

### Diagnostics

Per `tests-and-config.md` lines 455–463, the resolver returns `(LcmConfig, LcmConfigDiagnostics)` where diagnostics tracks **pattern-array provenance** for `ignoreSessionPatterns` and `statelessSessionPatterns`:

```python
class LcmConfigDiagnostics(BaseModel):
    ignore_session_patterns_source: Literal["env", "config", "default"]
    stateless_session_patterns_source: Literal["env", "config", "default"]
    ignore_session_patterns_env_overrides_config: bool
    stateless_session_patterns_env_overrides_config: bool
```

The `/lcm doctor` equivalent reads this to tell operators where each value came from.

### State-dir resolver

Port `resolveOpenclawStateDir(env)` (precedence `OPENCLAW_STATE_DIR` → `~/.openclaw`) and add a Hermes-side `resolve_hermes_state_dir(env)` (precedence `HERMES_HOME` → `~/.hermes`). The migration story (per ADR-003 + storage.md §10.1) reads from openclaw-state-dir on first launch and writes to hermes-state-dir.

## Dependencies

- Depends on: #00-01 (scaffolding — pydantic-settings dependency pinned in pyproject.toml).
- Blocks: #01-04 (migration reads `seedDefaultPrompts` flag from config; reads `largeFilesDir`), #01-08 / #01-09 (stores read context-threshold / fanout settings).

## Acceptance criteria

- [ ] `LcmConfig` pydantic model declares **all 52 in-scope fields** with TS defaults preserved.
- [ ] `resolve_lcm_config(env: Mapping[str, str], plugin_config: Mapping[str, Any]) -> LcmConfig` matches TS precedence: env > plugin-config > default.
- [ ] `resolve_lcm_config_with_diagnostics(...)` returns `(LcmConfig, LcmConfigDiagnostics)` with pattern-array source tracking.
- [ ] All **61 TS test cases** in `test/config.test.ts` have ported pytest equivalents in `tests/test_config.py` (per `tests-and-config.md` line 47 — mechanical port, no mocking required).
- [ ] `LCM_*` aliases emit a `DeprecationWarning` exactly once per env var per process (test with `pytest.warns(DeprecationWarning)`).
- [ ] `bootstrap_max_tokens = max(6000, leaf_chunk_tokens * 0.3)` derived-default fallback works when `LCM_BOOTSTRAP_MAX_TOKENS` is unset.
- [ ] `hot_cache_budget_headroom_ratio` clamps to `[0, 0.95]`; `critical_budget_pressure_ratio` clamps to `[0, 1]`; `hot_cache_pressure_factor` clamps to min 1 (per `tests-and-config.md` lines 295–298).
- [ ] `dynamic_leaf_chunk_tokens.max` defaults to `max(leaf_chunk_tokens, leaf_chunk_tokens * 2)` with floor = static `leaf_chunk_tokens`.
- [ ] `pytest tests/test_config.py` passes.
- [ ] `model_config = ConfigDict(extra="forbid")` rejects unknown YAML keys with a clear error citing the unknown key name.
- [ ] No new mypy errors (`mypy --strict`).
- [ ] PR description cites LCM commit `1f07fbd` and the source file path `src/db/config.ts`.

## Estimated effort

**8–12 hours** — bulk is the 61-case test port + the env-var enumeration + the diagnostics tuple. Logic is pure-function so no DB harness needed.

## Confidence

**95%** — pydantic-settings handles the env-override surface natively; the only place we drift from TS is the `LCM_*` deprecation-warning policy (documented in `tests-and-config.md` line 556, accepted policy).
