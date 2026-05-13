# Porting Guide: Plugin Glue + Slash Commands

**Source LOC:** ~3300 across plugin/ + index.ts + bridge (top-level `index.ts` 6 LOC, `src/plugin/index.ts` 2804 LOC, `src/plugin/lcm-command.ts` 2884 LOC, `src/plugin/shared-init.ts` 72 LOC, `src/plugin/lcm-db-backup.ts` 82 LOC, `src/plugin/lcm-doctor-shared.ts` 270 LOC, `src/openclaw-bridge.ts` 26 LOC)
**Python target LOC:** ~2500 (Hermes is leaner — fewer registration ceremonies; no `PluginCommandContext`; the gateway layer does owner-gating, not the command handler)
**Confidence target:** 95%
**Estimated effort:** 24–32 hours
**Epic:** 02-engine-skeleton (entry point + lifecycle wiring) + 08-cli-ops (slash commands)

## Architecture summary

`lossless-claw` is an **OpenClaw context-engine plugin** that registers in three orthogonal ways during one `register(api)` call:

1. **One context engine** — `api.registerContextEngine("lossless-claw", factory)` — replaces the host's default compactor.
2. **Eight tools** — `api.registerTool(factory, { name })` × 8 (`lcm_grep`, `lcm_describe`, `lcm_expand`, `lcm_expand_query`, `lcm_synthesize_around`, `lcm_get_entity`, `lcm_search_entities`, `lcm_compact`).
3. **One slash command with ~13 subcommands** — `api.registerCommand(createLcmCommand(...))` dispatches `/lcm` / `/lossless` to one of: `status`, `backup`, `rotate`, `health`, `worker status`, `worker tick embedding-backfill`, `doctor`, `doctor apply`, `doctor clean`, `doctor clean apply`, `reconcile-session-keys --list-candidates`, `reconcile-session-keys --apply`, `eval`, `purge`, `help`.

Plus four lifecycle hooks: `llm_output` (feed token-state cache), `before_reset` (handle `/new`), `before_prompt_build` (inject the LOSSLESS_RECALL_POLICY_PROMPT), `session_end` (flush per-session state). Plus two gateway-lifecycle hooks: `gateway_start` (retry deferred DB init on lock contention) and `gateway_stop` (close DB, kill autostart loops).

A **process-global singleton** (`shared-init.ts`) keyed by normalized DB path means that OpenClaw v2026.4.5+'s per-agent-context `register()` calls reuse the same DB connection and engine instance — without this, every subagent or cron lane spins up a fresh connection and migrations storm the DB. The shared object holds `getCachedEngine`, `waitForEngine`, `waitForDatabase`, plus optional autostart handles (backfill + extraction).

The TS surface is large because OpenClaw exposes a rich plugin API (`PluginCommandContext` has `sessionId`, `sessionKey`, `senderIsOwner`, `runtime.config.loadConfig()`, `runtime.agent.session.*`, fired hooks carry usage objects with provider/model/tokens). The Hermes port can be substantially leaner because:

- Hermes's `PluginContext.register_command(name, handler, ...)` takes `handler(raw_args: str) -> str | None`. There is no per-call context object. Owner-gating is enforced **before** dispatch by `gateway/slash_access.py` (admin allowlist) — the handler runs only if the user is authorized.
- Hermes's `ContextEngine` ABC already encapsulates `update_from_response`, `should_compress`, `compress`, `on_session_start`, `on_session_end`, `on_session_reset`, `get_tool_schemas`, `handle_tool_call`, `get_status`, `update_model`. Most of LCM's manual hook wiring (`llm_output` → `recordLlmOutput`, `before_reset` → `handleBeforeReset`) collapses into ABC method overrides.
- Hermes doesn't have an `OpenClawPluginApi.runtime` surface to mirror — the equivalent (config, auth profiles, agent/session API) lives on different Hermes modules (`hermes_cli.config`, `agent.plugin_llm`, etc.).

## File mapping

| TS | Python |
|---|---|
| `index.ts` (6 LOC top-level re-export) | drop — `pyproject.toml` entry-point points at `lossless_hermes:register` directly |
| `src/plugin/index.ts` (2804 LOC) | split → `src/lossless_hermes/__init__.py` (register + lifecycle, ~400 LOC) + `src/lossless_hermes/engine.py` (LcmContextEngine, ~400 LOC, ported from `src/engine.ts`) + `src/lossless_hermes/wiring.py` (hook closures, env snapshot, ~300 LOC) |
| `src/plugin/lcm-command.ts` (2884 LOC) | `src/lossless_hermes/commands/__init__.py` dispatcher + 9 subcommand modules under `commands/` (status, backup, rotate, doctor, doctor_cleaners, reconcile, worker, eval, purge, health, help) |
| `src/plugin/shared-init.ts` (72 LOC) | `src/lossless_hermes/shared_init.py` (~60 LOC; equivalent process-global registry keyed by normalized DB path) |
| `src/plugin/lcm-db-backup.ts` (82 LOC) | `src/lossless_hermes/db/backup.py` (~70 LOC; `VACUUM INTO`) |
| `src/plugin/lcm-doctor-shared.ts` (270 LOC) | `src/lossless_hermes/doctor/shared.py` (marker detection + target loading) |
| `src/openclaw-bridge.ts` (26 LOC) | `src/lossless_hermes/hermes_bridge.py` (~25 LOC; imports `ContextEngine` from `agent.context_engine`, `PluginContext` from `hermes_cli.plugins`) |
| `openclaw.plugin.json` (505 LOC manifest) | split → `plugin.yaml` (~30 LOC manifest) + `src/lossless_hermes/config.py` (~250 LOC pydantic schema mirroring `configSchema`) + `pyproject.toml` `[project.entry-points."hermes_agent.plugins"]` |

## Entry-point shape

**TS (OpenClaw):**

```ts
// index.ts (top-level)
export { default } from "./src/plugin/index.js";
```

```ts
// src/plugin/index.ts
const lcmPlugin = {
  id: "lossless-claw",
  name: "Lossless Context Management",
  description: "DAG-based conversation summarization ...",
  configSchema: { parse(value): LcmConfig { ... } },
  register(api: OpenClawPluginApi) {
    const registrationConfig = resolveRegistrationConfig(api);
    const deps = createLcmDependencies(api, registrationConfig);
    // ... singleton check, eager DB init or deferred-on-lock,
    // gateway_stop handler, wire context engine + 8 tools + 1 command + 4 hooks
  },
};
export default lcmPlugin;
```

OpenClaw's loader calls `lcmPlugin.register(api)` once per agent context (main, subagents, cron lanes).

**Python (Hermes):**

```python
# src/lossless_hermes/__init__.py
from pathlib import Path
from hermes_cli.plugins import PluginContext
from .config import resolve_lcm_config
from .engine import LcmContextEngine
from .commands import register_lcm_command
from .hooks import register_hooks
from .shared_init import get_shared_init, set_shared_init, normalize_db_path

def register(ctx: PluginContext) -> None:
    """Hermes plugin entry — called once per process (no per-subagent
    repeat call as in OpenClaw v2026.4.5+)."""
    cfg = resolve_lcm_config(ctx)
    db_path = normalize_db_path(cfg.database_path)

    # Singleton check: if a second register() arrived (currently only
    # possible via `discover_plugins(force=True)`), reuse the cached
    # engine so we don't open a second DB handle.
    shared = get_shared_init(db_path)
    if shared and not shared.stopped:
        _wire_handlers(ctx, shared, cfg)
        return

    shared = _initialize_engine(cfg)
    set_shared_init(db_path, shared)

    ctx.register_context_engine(shared.engine)
    register_hooks(ctx, shared)
    register_lcm_command(ctx, shared, cfg)
```

The Python entry-point has a much smaller eager/deferred-init footprint than the TS version. Hermes loads plugins once at CLI startup (not per agent context), so the deferred-on-lock retry that OpenClaw needs (because subagents race to open the same SQLite file) collapses to a single eager open with a clear failure path. The shared-init pattern is still worth keeping because `discover_plugins(force=True)` is a real codepath (used by long-lived gateway processes that need to rediscover after a config change).

## `openclaw.plugin.json` → `plugin.yaml` + `pyproject.toml` + `config.py`

The OpenClaw manifest carries four jobs in one file: plugin identity, activation, tool/UI contracts, and a TypeBox-style JSON Schema for config. In Hermes those four split:

### Job 1: plugin identity → `plugin.yaml`

OpenClaw `id`, `kind`, `activation` → Hermes `plugin.yaml`:

```yaml
name: lossless-hermes
version: 0.1.0
description: "DAG-based conversation summarization with incremental compaction, full-text search, and sub-agent expansion."
author: "@martian-engineering"
kind: exclusive   # context-engine category — see "Plugin kind" below.
provides_tools:
  - lcm_grep
  - lcm_describe
  - lcm_expand
  - lcm_expand_query
  - lcm_synthesize_around
  - lcm_get_entity
  - lcm_search_entities
  - lcm_compact
provides_hooks:
  - post_llm_call
  - on_session_reset
  - pre_llm_call
  - on_session_end
```

**Plugin kind decision (ADR-?):** Hermes's plugin system distinguishes `standalone`, `backend`, `exclusive` (memory providers — at most one active, selected via `<category>.provider` config), `platform`, `model-provider`. Context engines have their own discovery system at `plugins/context_engine/<name>/` (see `plugins/context_engine/__init__.py`), entirely separate from the general `PluginManager`. The cleanest port is to install lossless-hermes as `plugins/context_engine/lcm/` with a `register(ctx)` function — the existing `_EngineCollector` shim handles a `PluginContext`-shaped object that only implements `register_context_engine`. This bypasses the general plugin gating (`plugins.enabled`) and is selected via `context.engine: lcm` in `config.yaml`.

The alternative is registering through the general `PluginManager` as `kind: exclusive`, with selection via a new `context.provider` config key mirroring `memory.provider`. This is more uniform with the rest of Hermes but requires modifying `plugins.py:_VALID_PLUGIN_KINDS` and adding a `context-engine` category. **Recommended:** start with `plugins/context_engine/lcm/` and revisit if a user wants pip-installable context engines.

### Job 2: activation → `pyproject.toml`

OpenClaw `activation.onStartup: true` → Hermes entry-point in `pyproject.toml`:

```toml
[project.entry-points."hermes_agent.plugins"]
lossless-hermes = "lossless_hermes:register"
```

If shipped as a directory plugin at `plugins/context_engine/lcm/`, no entry-point is needed — the discovery loader at `plugins/context_engine/__init__.py:load_context_engine("lcm")` finds and instantiates it directly.

### Job 3: tool contracts → ABC

OpenClaw `contracts.tools: [...]` (declared) is checked at load by the host to enforce the plugin actually registers them. Hermes has no equivalent: tools are registered via `ctx.register_tool(...)` and reflected on the `ContextEngine.get_tool_schemas()` return. The `provides_tools` manifest field is advisory-only — used by `hermes plugins list` to show what's available.

### Job 4: config schema → pydantic models

The 505-LOC `openclaw.plugin.json` is half UI hints, half JSON Schema. The Hermes port should mirror the schema in pydantic, with field names converted to snake_case, and a separate `LcmUiHints` constant for the `hermes config` TUI to surface labels and help text:

```python
# src/lossless_hermes/config.py
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator

class CacheAwareCompactionConfig(BaseModel):
    enabled: bool = True
    cache_ttl_seconds: int = Field(default=300, ge=1)
    max_cold_cache_catchup_passes: int = Field(default=3, ge=1)
    hot_cache_pressure_factor: float = Field(default=1.5, ge=1.0)
    hot_cache_budget_headroom_ratio: float = Field(default=0.2, ge=0, le=0.95)
    cold_cache_observation_threshold: int = Field(default=2, ge=1)
    critical_budget_pressure_ratio: float = Field(default=0.70, ge=0, le=1)

class DynamicLeafChunkTokensConfig(BaseModel):
    enabled: bool = False
    max: int = Field(default=4000, ge=1)

class AutoRotateSessionFilesConfig(BaseModel):
    enabled: bool = False
    size_bytes: int = Field(default=2_097_152, ge=1)
    startup: Literal["rotate", "warn", "off"] = "warn"
    runtime: Literal["rotate", "warn", "off"] = "warn"

class FallbackProvider(BaseModel):
    provider: str
    model: str

class LcmConfig(BaseModel):
    enabled: bool = True
    context_threshold: float = Field(default=0.75, ge=0, le=1)
    incremental_max_depth: int = Field(default=-1, ge=-1)
    fresh_tail_count: int = Field(default=8, ge=1)
    fresh_tail_max_tokens: int = Field(default=0, ge=0)
    prompt_aware_eviction: bool = False
    leaf_chunk_tokens: int = Field(default=2000, ge=1)
    bootstrap_max_tokens: int = Field(default=20_000, ge=1)
    new_session_retain_depth: int = Field(default=-1, ge=-1)
    leaf_target_tokens: int = Field(default=300, ge=1)
    condensed_target_tokens: int = Field(default=500, ge=1)
    max_expand_tokens: int = Field(default=8000, ge=1)
    leaf_min_fanout: int = Field(default=4, ge=2)
    condensed_min_fanout: int = Field(default=3, ge=2)
    condensed_min_fanout_hard: int = Field(default=2, ge=2)
    database_path: Optional[str] = None  # default: $HERMES_HOME/lcm.db
    large_files_dir: Optional[str] = None  # default: $HERMES_HOME/lcm-files
    ignore_session_patterns: list[str] = Field(default_factory=list)
    stateless_session_patterns: list[str] = Field(default_factory=list)
    skip_stateless_sessions: bool = False
    large_file_threshold_tokens: int = Field(default=10_000, ge=1000)
    summary_model: str = ""
    summary_provider: str = ""
    large_file_summary_model: str = ""
    large_file_summary_provider: str = ""
    expansion_model: str = ""
    expansion_provider: str = ""
    delegation_timeout_ms: int = Field(default=120_000, ge=1)
    summary_timeout_ms: int = Field(default=60_000, ge=1)
    max_assembly_token_budget: int = Field(default=200_000, ge=1000)
    tool_result_token_budget: int = Field(default=10_000, ge=2000)
    summary_max_overage_factor: float = Field(default=3.0, ge=1)
    custom_instructions: str = ""
    circuit_breaker_threshold: int = Field(default=3, ge=1)
    circuit_breaker_cooldown_ms: int = Field(default=300_000, ge=1)
    cache_aware_compaction: CacheAwareCompactionConfig = Field(default_factory=CacheAwareCompactionConfig)
    dynamic_leaf_chunk_tokens: DynamicLeafChunkTokensConfig = Field(default_factory=DynamicLeafChunkTokensConfig)
    timezone: str = ""
    prune_heartbeat_ok: bool = False
    transcript_gc_enabled: bool = False
    agent_compaction_tool_enabled: bool = False  # default false — operator opt-in
    proactive_threshold_compaction_mode: Literal["deferred", "inline"] = "deferred"
    auto_rotate_session_files: AutoRotateSessionFilesConfig = Field(default_factory=AutoRotateSessionFilesConfig)
    fallback_providers: list[FallbackProvider] = Field(default_factory=list)

    class Config:
        # OpenClaw schema sets additionalProperties:false; mirror that.
        extra = "forbid"
```

Config delivery (ADR-?): OpenClaw reads `plugins.entries["lossless-claw"].config` from the validated runtime config. Hermes's equivalent is `cfg_get(config, "context", "lcm", default={})` — i.e. `config.yaml` carries a `context.lcm: { ... }` block, parsed by `LcmConfig.model_validate()`. The alternative is a stand-alone `~/.hermes/lcm.yaml`, but in-tree convention (memory, image_gen) uses the main config — recommended to follow.

### Field renames + Hermes-side adjustments

| OpenClaw | Hermes |
|---|---|
| `dbPath` (with `databasePath` alias) | `database_path` only (pydantic doesn't need alias; surface the canonical name) |
| `largeFilesDir` defaults to `<OPENCLAW_STATE_DIR>/lcm-files` | defaults to `<HERMES_HOME>/lcm-files` |
| `summaryModel: "gpt-5.4"` (bare) OR `"openai-resp/gpt-5.4"` (provider/model) | use Hermes's existing provider model ref form — see Epic 02 model resolution |
| `LCM_SUMMARY_MODEL` env override | keep as-is; same semantic (process env beats config) |
| `OPENCLAW_STATE_DIR` env | `HERMES_HOME` env (`hermes_constants.get_hermes_home()`) |
| `OPENCLAW_PROVIDER` env (default provider) | Hermes's default provider resolution lives in `providers/__init__.py:get_provider_profile()`; no single env var |

## Plugin registration sequence

The TS register flow (concrete order from `src/plugin/index.ts:wirePluginHandlers`):

1. Subscribe `llm_output` → `recordLlmOutput` (feeds per-session token-state cache, used by tools at `execute()` time).
2. Subscribe `before_reset` → `engine.handleBeforeReset` (handles `/new`).
3. Subscribe `before_prompt_build` → return `{ prependSystemContext: LOSSLESS_RECALL_POLICY_PROMPT }` (a static ~3000-char policy block telling the agent how to use lcm_* tools).
4. Subscribe `session_end` → `engine.handleSessionEnd`.
5. `api.registerContextEngine("lossless-claw", factory)` — the factory is sync-or-async; returns the engine or a promise resolving to it.
6. `api.registerTool` × 8 (each takes a per-ctx factory plus `{ name }`).
7. `api.registerCommand(createLcmCommand(...))` — single command with subcommand dispatch.

Plus, OUTSIDE `wirePluginHandlers` (in the `register()` body after wiring):

8. `api.on("gateway_stop", ...)` — close DB, kill autostart loops, remove from shared init store.
9. If eager DB init failed with `database is locked`: `api.on("gateway_start", ...)` to retry once.
10. Fire-and-forget: `tryStartBackfillAutostart(db)` (Voyage embedding backfill loop, opt-in via `VOYAGE_API_KEY`).
11. Fire-and-forget: `tryStartExtractionAutostart(db, deps)` (LLM-backed entity coreference loop, default on, opt-out via `LCM_EXTRACTION_LLM_ENABLED=false`).
12. `logStartupBannerOnce("plugin-loaded" | "state-dir" | "compaction-model" | "fallback-providers")` — startup banners deduped via `Symbol.for()`-keyed globalThis cache.

**Python skeleton (concrete order matching TS where it matters):**

```python
# src/lossless_hermes/__init__.py

import logging
from typing import Any, Awaitable, Callable

from hermes_cli.plugins import PluginContext
from .config import resolve_lcm_config, LcmConfig
from .engine import LcmContextEngine
from .commands import register_lcm_command
from .shared_init import get_shared_init, set_shared_init, normalize_db_path, SharedLcmInit
from .startup_banner import log_startup_banner_once

LOGGER = logging.getLogger(__name__)

LOSSLESS_RECALL_POLICY_PROMPT = """## Lossless Recall Policy
The lossless-hermes plugin is active.
For compacted conversation history, these instructions supersede generic memory-recall guidance.
... (full prompt; ported verbatim from src/plugin/index.ts:LOSSLESS_RECALL_POLICY_PROMPT)
"""

def register(ctx: PluginContext) -> None:
    cfg = resolve_lcm_config(ctx)
    if not cfg.enabled:
        LOGGER.info("[lcm] disabled via config")
        return

    db_path = normalize_db_path(cfg.database_path)

    # 0. Singleton check (only fires on discover_plugins(force=True);
    # Hermes doesn't have per-subagent register() like OpenClaw v2026.4.5+).
    existing = get_shared_init(db_path)
    if existing and not existing.stopped:
        LOGGER.info("[lcm] reusing shared engine init for db=%s", db_path)
        _wire_handlers(ctx, existing, cfg)
        return

    # 1. Eager DB + engine init.
    engine = LcmContextEngine(cfg)  # opens DB, runs migrations, builds DAG store
    shared = SharedLcmInit(
        engine=engine,
        stopped=False,
        backfill_autostart=None,
        extraction_autostart=None,
    )
    set_shared_init(db_path, shared)

    # 2-7. Wire context engine + tools + command + hooks.
    _wire_handlers(ctx, shared, cfg)

    # 8. on_session_end / on_session_finalize → cleanup handled inside
    # the engine's own ABC overrides (no separate gateway_stop hook
    # needed; Hermes plugin lifecycle is process-bound).
    # If we need an explicit "process exiting" hook, Hermes provides
    # atexit which we can register from inside the engine.

    # 9. (TS gateway_start retry omitted — Hermes single-process loader
    # doesn't race the DB.)

    # 10-11. Autostart loops (best-effort; both gated on their respective
    # env vars + DB feature checks).
    shared.backfill_autostart = _try_start_backfill_autostart(engine.db)
    shared.extraction_autostart = _try_start_extraction_autostart(engine.db, engine.deps)

    # 12. Startup banners (deduped via process-global set).
    log_startup_banner_once("plugin-loaded", f"[lcm] Plugin loaded (db={cfg.database_path}, threshold={cfg.context_threshold})")
    log_startup_banner_once("state-dir", f"[lcm] State dir: {cfg.database_path}")
    log_startup_banner_once("compaction-model", _build_compaction_model_log(cfg))


def _wire_handlers(ctx: PluginContext, shared: SharedLcmInit, cfg: LcmConfig) -> None:
    # 1. post_llm_call: capture usage, feed token-state cache.
    #    OpenClaw "llm_output" → Hermes "post_llm_call".
    def _on_post_llm_call(*, response: Any, session_id: str, **kw: Any) -> None:
        usage = getattr(response, "usage", None) or response.get("usage") if isinstance(response, dict) else None
        if usage:
            shared.engine.record_llm_output(session_id, usage)
    ctx.register_hook("post_llm_call", _on_post_llm_call)

    # 2. on_session_reset: handle /new and /reset.
    #    OpenClaw "before_reset" → Hermes "on_session_reset".
    #    NOTE: this is also called automatically on every ContextEngine
    #    via the ABC, so the manual hook is for behavior that needs to
    #    happen BEFORE the engine's own reset clears state.
    def _on_session_reset(**kw: Any) -> None:
        shared.engine.handle_before_reset(reason=kw.get("reason"), session_id=kw.get("session_id"))
    ctx.register_hook("on_session_reset", _on_session_reset)

    # 3. pre_llm_call: inject the LOSSLESS_RECALL_POLICY_PROMPT.
    #    OpenClaw "before_prompt_build" returning prependSystemContext →
    #    Hermes "pre_llm_call" returning a dict {"context": "..."}.
    #    IMPORTANT divergence: OpenClaw prepends to SYSTEM; Hermes
    #    injects to USER (see hermes_cli/plugins.py:invoke_hook docstring
    #    — "Context is ALWAYS injected into the user message, never the
    #    system prompt" to preserve prompt cache). The policy text needs
    #    minor rewording so it still reads correctly as a user-message
    #    preamble. ADR needed.
    def _on_pre_llm_call(**kw: Any) -> dict:
        return {"context": LOSSLESS_RECALL_POLICY_PROMPT}
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    # 4. on_session_end: flush per-session state.
    def _on_session_end(*, session_id: str, **kw: Any) -> None:
        shared.engine.handle_session_end(session_id=session_id, **kw)
    ctx.register_hook("on_session_end", _on_session_end)

    # 5. Register context engine. Hermes accepts at most one; second
    #    registration is rejected with a warning.
    ctx.register_context_engine(shared.engine)

    # 6. Tools: lcm_grep, lcm_describe, lcm_expand, lcm_expand_query,
    #    lcm_synthesize_around, lcm_get_entity, lcm_search_entities,
    #    lcm_compact. Each is a (name, toolset, schema, handler) tuple.
    #    See Epic 02 (engine skeleton) / Epic 06 (tool surface) for full
    #    handler signatures.
    from .tools import register_lcm_tools
    register_lcm_tools(ctx, shared.engine, cfg)

    # 7. Slash command: /lcm with subcommand dispatch.
    register_lcm_command(ctx, shared, cfg)
```

## OpenClaw lifecycle hooks → Hermes lifecycle hooks

This is the highest-confidence mapping in the port — every OpenClaw hook used by LCM has a direct Hermes equivalent:

| OpenClaw event (TS) | Hermes hook (Python) | Notes |
|---|---|---|
| `llm_output` | `post_llm_call` | Both fire AFTER the LLM call completes; both carry usage. Hermes also has `post_api_request` if you need a lower-level view. |
| `before_reset` | `on_session_reset` | `/new` and `/reset`. The Hermes ContextEngine ABC also has `on_session_reset()` method which is called automatically — register the hook only if you need to run BEFORE the engine's own reset. |
| `before_prompt_build` returning `{prependSystemContext: "..."}` | `pre_llm_call` returning `{"context": "..."}` or a plain string | **CRITICAL DIVERGENCE:** OpenClaw prepends to the **system prompt**; Hermes injects to the **user message** to preserve prompt-cache prefix. Wording of the recall-policy prompt must work in either position. See ADR-? below. |
| `session_end` (with `reason`, `sessionId`, `sessionKey`, `nextSessionId`, `nextSessionKey`) | `on_session_end` (with `session_id`, `messages`) | Hermes passes the final message list; OpenClaw doesn't. Hermes lacks `nextSessionId` — port may need to track this internally if engine state transfer matters. |
| `gateway_start` (deferred-init retry on DB lock) | (omit) | Hermes single-process plugin load doesn't race the DB. If lock contention shows up, expose a retry via `discover_plugins(force=True)` or add a startup-completion hook. |
| `gateway_stop` (close DB, kill autostart) | `atexit.register(...)` or no-op | Hermes process exit handles SQLite handles via the OS. Autostart loops should still be cancelled cleanly — register `atexit` from inside the engine constructor. |

**Hermes-only hooks LCM might want:**

- `pre_tool_call` — block calls in flight (e.g. rate-limit `lcm_expand_query` if the agent is burning the delegation budget).
- `post_tool_call` — log tool outcomes to `lcm_tool_audit`.
- `transform_tool_result` — apply the result-budget truncation (`tool_result_token_budget`) before the agent sees the tool output.
- `pre_approval_request` / `post_approval_response` — observe approval flows for telemetry.

**Hermes hooks LCM should NOT register:**

- `transform_llm_output` — meant for voice/personality rewrites; not needed for LCM.
- `pre_gateway_dispatch` — meant for message filtering; not LCM's concern.

## `/lcm` slash commands — full inventory

There are **13 distinct command shapes**, all dispatched through one `parseLcmCommand` switch. The TS plugin registers ONE OpenClaw command (`name: "lcm"`, native name `lossless`) with `acceptsArgs: true`; the handler reads `ctx.args` and switches on the first token. The same pattern works in Hermes.

| Subcommand | Args | Owner-gated | Purpose | Python target |
|---|---|---|---|---|
| `/lcm` (no args) | — | no | Alias for `status` | `commands/status.py:run` |
| `/lcm status` | — | no | Full LCM health snapshot (conversation count, summary count, stored vs source tokens, leaf/condensed counts, current conversation context-token + compression ratio, last maintain telemetry). | `commands/status.py:run` |
| `/lcm backup` | — | no | `VACUUM INTO` to `<db>.<timestamp>-<rand>.bak`. Read-only writer (creates a new file; doesn't mutate the DB). | `commands/backup.py:run` |
| `/lcm rotate` | — | no | Rotate the current session JSONL transcript (creates timestamped backup, replaces with bootstrap+fresh-tail). Calls `engine.rotateSessionStorageWithBackup` with 30s DB lock timeout. | `commands/rotate.py:run` |
| `/lcm health` | — | no | v4.1 health snapshot — worker statuses, embedding backfill pending count, active embedding model, hybrid-search FTS+vec0 check. Calls `getV41HealthSnapshot(db)`. | `commands/health.py:run` |
| `/lcm worker` (no args) or `/lcm worker status` | — | no | Worker status table (embedding backfill, entity extraction, themes/procedures deferred). Calls `getWorkerStatusSnapshot(db)`. | `commands/worker.py:run_status` |
| `/lcm worker tick embedding-backfill` | — | **YES** | Force one tick of the embedding backfill worker (200 paid Voyage embeddings per call). Owner-gated because of paid quota burn. | `commands/worker.py:run_tick_backfill` |
| `/lcm doctor` (no args or `--apply` omitted) | — | no | Read-only doctor scan — finds summaries with fallback/truncated markers (`detectDoctorMarker` in `lcm-doctor-shared.ts`). | `commands/doctor.py:run_scan` |
| `/lcm doctor apply` | — | **YES** | Repair pass — re-summarizes broken summaries by calling the active summarizer (costs LLM tokens). Mutates `summaries.content`. | `commands/doctor.py:run_apply` |
| `/lcm doctor clean` | — | **YES** | Read-only listing of high-confidence junk candidates from `lcm-doctor-cleaners.ts`. **Note:** Wave-12 P1 fix gated even the read-only listing because it exposes `session_key` + first-message previews across all conversations. | `commands/doctor.py:run_cleaners_scan` |
| `/lcm doctor clean apply [filter-id] [vacuum]` | `filter-id` (one of the doctor-cleaner IDs); `vacuum` literal | **YES** | Apply the doctor cleaners (destructive — DELETEs rows). Optional `vacuum` runs SQLite VACUUM after. | `commands/doctor.py:run_cleaners_apply` |
| `/lcm reconcile-session-keys --list-candidates` | — | **YES** (Wave-12 P1 fix) | Read-only listing of "session keys that look like duplicates / drift candidates". Gated because it exposes session keys + previews. | `commands/reconcile.py:run_list` |
| `/lcm reconcile-session-keys --apply --from k1,k2 --to k3 --reason "..." [--allow-main-session]` | `--from` comma-list, `--to` single, `--reason` quoted, `--allow-main-session` bool | **YES** | Rewrites `session_key` on `conversations` + `summaries`. With `--allow-main-session` can target Eva's primary thread. | `commands/reconcile.py:run_apply` |
| `/lcm eval [--baseline] [--mode <fts_only\|semantic_only\|hybrid>] [--query-set <name>] [--version <int>]` | flag-driven; requires `--baseline` OR `--mode` | **YES** | Run the eval harness against `lcm_eval_run` + `lcm_eval_query_result`. Hybrid mode embeds the query (paid Voyage cost). | `commands/eval.py:run` |
| `/lcm purge --reason "..." [--session-key <k>] [--summary-ids id1,id2] [--since <iso>] [--before <iso>] [--min-token-count <n>] [--allow-main-session] [--apply]` | quoted reason required, at least one scope criterion required | **YES** | Soft-suppress leaves + cascade to messages, vec0, synthesis cache. Default dry-run; `--apply` commits. | `commands/purge.py:run` |
| `/lcm help` | — | no | Help text. | `commands/help.py:run` |

**Owner-gating count:** 9 out of 13 subcommands are owner-only. The 4 non-gated ones (`status`, `backup`, `rotate`, `health`, `worker status`, `doctor` read-only, `help`) are safe for any agent to call — they're read-only on data the caller already has access to.

**Aliases:**

- TS: `name: "lcm"`, `nativeNames.default: "lossless"` → the canonical command is `/lossless` and `/lcm` is an alias.
- Port: Hermes's `register_command` doesn't take aliases natively. Register `/lcm` (the more-typed name) as canonical and add a Python-side alias dispatch: register `/lossless` separately, both pointing at the same handler.

**Hidden Telegram surface:** OpenClaw shows `/lossless` in the Telegram menu and hides `/lcm`. Hermes's Telegram menu sources commands from `register_command` registrations (see `hermes_cli/commands.py:telegram_bot_commands`); since both names appear, both show up unless filtered.

## Owner-gating in Hermes

**OpenClaw mechanism:** Every `OpenClawPluginCommandDefinition.handler` receives `ctx: PluginCommandContext` containing `ctx.senderIsOwner: boolean`. The TS LCM handler checks this inside each subcommand case and returns a JSON-formatted "operator-only" rejection text. The host runs the handler regardless — the gate is the plugin's responsibility.

**Hermes mechanism:** Owner-gating is **upstream of the handler**. The gateway layer (`gateway/run.py:8270`) computes `policy = policy_for_source(self.config, source)` from `allow_admin_from` in `config.yaml`, and rejects the command BEFORE dispatching to the plugin handler. Non-admin users get `"⛔ /lcm is admin-only here. ..."` and the handler is never called.

This means the **Python port doesn't need per-subcommand `senderIsOwner` checks if the operator configures `allow_admin_from` correctly**. But there are caveats:

1. **CLI mode has no slash-access policy.** When LCM runs under `hermes` (CLI, not gateway), there's no `gateway_config` or `source.user_id` — `policy_for_source` returns `enabled=False` and every call is admin. CLI is implicitly single-user-owner, so this is fine.

2. **If `allow_admin_from` is unset on a platform, gating is disabled for that scope** — all allowed users get all commands. This is OpenClaw's `senderIsOwner=false` case but Hermes treats it as `is_admin=true` by default. **This is a security regression for the port unless the operator explicitly sets `allow_admin_from`.** The port should add a defense-in-depth check inside owner-gated subcommands:

```python
# src/lossless_hermes/commands/_gate.py
from typing import Callable
from hermes_cli.plugins import PluginContext  # not actually receivable in handler

# Hermes plugin handlers receive only raw_args. To do an in-handler
# owner check we need to reach into the active session state. The
# cleanest hook is a module-global "current request context" set by
# the gateway just before dispatch. See ADR-? below.
def is_owner_call() -> bool:
    from agent.runtime import current_session  # hypothetical Hermes API
    return current_session().is_owner

def require_owner(handler: Callable[[str], str | None]) -> Callable[[str], str | None]:
    def wrapped(raw_args: str) -> str | None:
        if not is_owner_call():
            return _operator_only_rejection_text()
        return handler(raw_args)
    return wrapped
```

**The cleanest path:** rely on `gateway/slash_access.py` for the primary gate, and document the requirement that operators set `allow_admin_from` for every platform that runs LCM. Skip the in-handler defense-in-depth UNLESS Hermes provides a `request_context` thread-local — currently it doesn't.

**ADR-? required:** Owner-gating mechanism for `/lcm` subcommands.
- **Option A** — pure upstream gate via `gateway/slash_access.py`. Operator must set `allow_admin_from` + add destructive subcommands to a denylist for non-admins. **No** in-handler check.
- **Option B** — request a `request_context` thread-local in Hermes core, then add per-subcommand `if not request_context.is_owner: return rejection_text` checks mirroring the TS. Defense-in-depth.
- **Option C** — register destructive subcommands as **separate slash commands** (`/lcm-purge`, `/lcm-doctor-apply`, etc.) so operators can put them in different `allow_admin_from` / `user_allowed_commands` tiers. More surface area but maps cleanly to Hermes's gating model.

**Recommended:** A for v1 (smallest surface; document the operator requirement clearly); revisit if security review wants depth.

## `openclaw-bridge.ts` replacement

The 26-LOC bridge re-exports from `openclaw/plugin-sdk` so plugin code can import stable types:

```ts
// src/openclaw-bridge.ts
export type {
  AnyAgentTool, ContextEngine, ContextEngineInfo, AssembleResult,
  CompactResult, IngestResult, IngestBatchResult, BootstrapResult,
  OpenClawPluginApi, OpenClawPluginCommandDefinition, PluginCommandContext,
  SubagentSpawnPreparation, SubagentEndReason,
} from "openclaw/plugin-sdk";
export { registerContextEngine, type ContextEngineFactory } from "openclaw/plugin-sdk";
```

Python replacement:

```python
# src/lossless_hermes/hermes_bridge.py
"""Compatibility bridge for Hermes plugin SDK symbols. Keeps the rest of
lossless-hermes importable without knowing the exact Hermes module
layout — same intent as src/openclaw-bridge.ts in the TS source.
"""
from agent.context_engine import ContextEngine
from hermes_cli.plugins import PluginContext, VALID_HOOKS

__all__ = ["ContextEngine", "PluginContext", "VALID_HOOKS"]
```

OpenClaw's bridge also re-exports several DTO types (`AssembleResult`, `CompactResult`, etc.) that the TS engine returns. The Hermes port doesn't need them at the bridge level — `ContextEngine.compress()` returns `List[Dict[str, Any]]` (the new message list), full stop. If the port wants typed result DTOs internally, define them in `src/lossless_hermes/types.py` rather than the bridge.

## Shared-init singleton port

Direct translation of `src/plugin/shared-init.ts`:

```python
# src/lossless_hermes/shared_init.py
"""Process-global singleton for LCM plugin initialization.

OpenClaw v2026.4.5+ called plugin register() per-agent-context; Hermes
calls register() once per process. The singleton is kept for
discover_plugins(force=True) (long-lived gateway processes that
rediscover after config changes) — without it, force=True would open
a second DB connection and run migrations concurrently.

Keyed by normalized DB path so the same plugin file with two configs
pointing at different DBs gets two independent engines.
"""
from __future__ import annotations
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .engine import LcmContextEngine

@dataclass
class SharedLcmInit:
    engine: LcmContextEngine
    stopped: bool = False
    backfill_autostart: Optional[Any] = None
    extraction_autostart: Optional[Any] = None

_lock = threading.Lock()
_store: dict[str, SharedLcmInit] = {}

def normalize_db_path(db_path: str | Path) -> str:
    return str(Path(db_path).expanduser().resolve())

def get_shared_init(db_path: str) -> Optional[SharedLcmInit]:
    with _lock:
        return _store.get(db_path)

def set_shared_init(db_path: str, init: SharedLcmInit) -> None:
    with _lock:
        _store[db_path] = init

def remove_shared_init(db_path: str) -> None:
    with _lock:
        _store.pop(db_path, None)

def clear_all_shared_init() -> None:
    """Test-only — clear all shared init state."""
    with _lock:
        _store.clear()
```

Python's `dict` is thread-safe for primitive ops but we add an explicit lock for clarity. Python doesn't need `Symbol.for()` because `__main__` module identity is already process-global.

## Test inventory

TS plugin tests (in `test/`):
- `test/plugin-prompt-hook.test.ts` — `before_prompt_build` returns `prependSystemContext` correctly. **Port target:** `tests/test_pre_llm_call_hook.py` — verify the user-message injection (NOT system prompt — Hermes divergence) emits the right policy text.
- `test/plugin-config-registration.test.ts` — config schema parse + defaults. **Port target:** `tests/test_lcm_config.py` — pydantic round-trip + invalid value rejection.

LCM command tests are NOT separated into a single dedicated file — the TS source has them inline in `lcm-command.ts:__testing` plus various scenario tests. The port should add per-subcommand tests:

- `tests/commands/test_parse_lcm_command.py` — token splitter for `--reason "..."` quoting, valid/invalid args per subcommand.
- `tests/commands/test_status_text.py` — exact-match expected rendering (snapshot test).
- `tests/commands/test_backup.py` — `VACUUM INTO` produces a non-zero file at the expected path.
- `tests/commands/test_rotate.py` — rotates JSONL with backup, 30s lock timeout, bootstrap+fresh-tail emitted.
- `tests/commands/test_doctor_scan_and_apply.py` — detects fallback markers, apply re-summarizes via injected mock summarizer.
- `tests/commands/test_purge.py` — soft-suppress cascade + audit row, `--apply` vs dry-run, `--allow-main-session` requirement.
- `tests/commands/test_reconcile.py` — list candidates, apply rewrites session_key on both tables + audit row.
- `tests/commands/test_eval.py` — fts_only / semantic_only / hybrid modes; eval-run row written.
- `tests/commands/test_worker.py` — status snapshot; backfill tick increments processed count.
- `tests/commands/test_owner_gating.py` — destructive subcommands reject when slash-access policy denies them.

## Config schema — every knob LCM exposes

Full list (44 top-level config keys, plus nested objects in `cacheAwareCompaction` (7), `dynamicLeafChunkTokens` (2), `autoRotateSessionFiles` (4), `fallbackProviders` (array of 2-field objects)):

`enabled`, `contextThreshold`, `incrementalMaxDepth`, `freshTailCount`, `freshTailMaxTokens`, `promptAwareEviction`, `leafChunkTokens`, `bootstrapMaxTokens`, `newSessionRetainDepth`, `leafTargetTokens`, `condensedTargetTokens`, `maxExpandTokens`, `leafMinFanout`, `condensedMinFanout`, `condensedMinFanoutHard`, `dbPath`, `databasePath` (alias), `largeFilesDir`, `ignoreSessionPatterns`, `statelessSessionPatterns`, `skipStatelessSessions`, `largeFileThresholdTokens`, `largeFileTokenThreshold` (legacy alias), `summaryModel`, `summaryProvider`, `largeFileSummaryModel`, `largeFileSummaryProvider`, `expansionModel`, `expansionProvider`, `delegationTimeoutMs`, `summaryTimeoutMs`, `maxAssemblyTokenBudget`, `toolResultTokenBudget`, `summaryMaxOverageFactor`, `customInstructions`, `circuitBreakerThreshold`, `circuitBreakerCooldownMs`, `cacheAwareCompaction.{enabled, cacheTTLSeconds, maxColdCacheCatchupPasses, hotCachePressureFactor, hotCacheBudgetHeadroomRatio, coldCacheObservationThreshold, criticalBudgetPressureRatio}`, `dynamicLeafChunkTokens.{enabled, max}`, `timezone`, `pruneHeartbeatOk`, `transcriptGcEnabled`, `agentCompactionToolEnabled`, `proactiveThresholdCompactionMode` (enum: deferred|inline), `autoRotateSessionFiles.{enabled, sizeBytes, startup, runtime}` (startup/runtime: rotate|warn|off), `fallbackProviders` (array of `{provider, model}`).

See `LcmConfig` pydantic model above for the full Python mapping with defaults, snake_case names, and constraint validators (`ge`, `le`, `Literal`).

**Env vars consumed by the plugin (must be ported):**

- `LCM_SUMMARY_MODEL` — overrides `summary_model` if set.
- `LCM_SUMMARY_PROVIDER` — overrides `summary_provider`.
- `LCM_TOOL_RESULT_TOKEN_BUDGET` — overrides `tool_result_token_budget`.
- `LCM_EXTRACTION_LLM_ENABLED` — `false` opts out of extraction autostart (default on).
- `VOYAGE_API_KEY` — opts INTO backfill autostart (default off).
- `OPENCLAW_STATE_DIR` → port to `HERMES_HOME` (resolved via `hermes_constants.get_hermes_home()`).
- `OPENCLAW_PROVIDER` → port to Hermes default-provider resolution (no single env var).
- `OPENCLAW_AGENT_DIR` / `PI_CODING_AGENT_DIR` — fallback agent state dir; in Hermes this should fall back to `HERMES_HOME` directly.

## OpenClaw → Hermes API translation per subcommand

For each subcommand, the translation pattern is identical:

```python
# OpenClaw side (TS):
case "status":
  return { text: await buildStatusText({ ctx, db: await getDb(), config }) };

# Hermes side (Python):
def _status_handler(raw_args: str) -> str:
    # No ctx — pull session state from a module-global or from
    # PluginManager._cli_ref.agent if needed (rare for /lcm).
    db = _shared.engine.db  # closure over SharedLcmInit
    return build_status_text(db=db, config=_cfg)
```

The TS `ctx.sessionId` / `ctx.sessionKey` (used by `/lcm status` to show current-conversation stats) is the one piece of context not directly available in Hermes's handler. Workarounds:

1. **Read from active engine state** — `shared.engine.current_session_id` (the engine tracks this via `on_session_start`).
2. **Read from CLI/agent reference** — `PluginManager._cli_ref.agent.session_id` (works in CLI mode; None in gateway mode).
3. **Pass via closure** — register a handler factory that closes over the resolved `current_session_provider()` lambda.

For `/lcm status`, all three work but #1 is cleanest. For `/lcm rotate` (needs the session's JSONL file path), the engine must expose `engine.get_session_file_path(session_id)` rather than relying on a runtime `api.runtime.agent.session.resolveSessionFilePath`.

**Per-subcommand translation table:**

| TS handler input (`ctx`) | Hermes handler input | Reach for |
|---|---|---|
| `ctx.args` | `raw_args: str` | Direct. |
| `ctx.sessionId`, `ctx.sessionKey` | (none) | `shared.engine.current_session_id`. |
| `ctx.senderIsOwner` | (none in handler) | Upstream `slash_access.policy.is_admin(source.user_id)`; defense-in-depth optional (see ADR-? above). |
| `api.runtime.config.loadConfig()` | (none in handler) | Closure over `cfg: LcmConfig` resolved at register time. |
| `api.runtime.agent.session.resolveSessionFilePath(sessionId, entry, opts)` | (none) | `shared.engine.get_session_file_path(session_id)` — engine method that wraps the Hermes-internal session-store API. |

## OpenClaw plugin lifecycle hooks the engine itself overrides (not separate hook registrations)

Several OpenClaw hooks that LCM treats as plugin-API hooks have Hermes equivalents on the **ContextEngine ABC** rather than the PluginContext hook registry. Mapping:

| OpenClaw `api.on(...)` | Hermes `ContextEngine.<method>()` |
|---|---|
| `llm_output` (token usage update) | `update_from_response(usage)` (called automatically by `agent/llm_client.py` after each call) |
| `before_reset` | `on_session_reset()` (called by `/new` and `/reset` handlers; ABC has default implementation that resets token counters) |
| `session_end` | `on_session_end(session_id, messages)` |
| `before_prompt_build` (system context injection) | (no ABC method — use `pre_llm_call` hook on PluginContext) |
| `gateway_stop` | (no ABC method — register `atexit` from `__init__`) |

**Recommended split:** put `update_from_response`, `on_session_reset`, `on_session_end` on the engine class (ABC overrides). Put `pre_llm_call` (policy prompt injection) on the PluginContext via `register_hook`. Don't double-register the same behavior on both.

## Open decisions

- **ADR-?: plugin kind + discovery layout.**
  - Option A — directory plugin at `plugins/context_engine/lcm/` (Hermes-existing discovery; recommended for v1).
  - Option B — pip-installable via `hermes_agent.plugins` entry-point with `kind: exclusive` and a new `context.provider` selection key (uniform with memory providers; requires Hermes-core changes).

- **ADR-?: config delivery.**
  - Option A — `config.yaml` under `context.lcm: { ... }` (recommended; matches memory/image_gen convention).
  - Option B — stand-alone `~/.hermes/lcm.yaml`.

- **ADR-?: owner-gating mechanism for destructive `/lcm` subcommands.**
  - Option A — upstream-only via `gateway/slash_access.allow_admin_from`; document operator requirement (recommended for v1).
  - Option B — request `request_context` thread-local in Hermes core; do per-subcommand `is_owner` checks.
  - Option C — split destructive subcommands into separate slash commands (`/lcm-purge`, `/lcm-doctor-apply`) so operators can tier-gate them individually.

- **ADR-?: policy-prompt injection point.**
  - Option A — inject as user-message context via `pre_llm_call` (Hermes-conventional; preserves prompt-cache prefix; reword the policy text to read as a user-message preamble).
  - Option B — propose `pre_system_prompt_build` hook to Hermes core (true equivalent to OpenClaw `before_prompt_build prependSystemContext`; breaks Hermes's prompt-cache invariant unless the cached prefix is rebuilt to include it).

- **ADR-?: aliases and command naming.**
  - Canonical: `/lcm`. Add `/lossless` alias by registering it as a separate command pointing at the same handler. Document the canonical/alias relationship in `provides_commands` (which Hermes manifest lacks — would need a comment).

- **ADR-?: backfill + extraction autostart loops as plugin-internal threads vs Hermes cron.**
  - Option A — keep the in-process loops, mirror the TS pattern, register cancellation via `atexit` (recommended; simplest port).
  - Option B — register as Hermes cron jobs (uses existing scheduler; nicer ops; bigger port surface).

## Remaining 5% risk

1. **`PluginCommandContext.sessionId` in CLI mode.** TS `/lcm status` and `/lcm rotate` use `ctx.sessionId` / `ctx.sessionKey` to resolve current-conversation stats. Hermes handler has no such ctx. Port via engine-internal `current_session_id` works for `status`; rotate is fine because the JSONL path is engine-managed; but if `lcm-command.ts` uses `ctx.sessionKey` for finer-grained reasoning (e.g. matching a `stateless_session_patterns` glob) the port needs to verify that path. **Mitigation:** dry-run the port with all 13 subcommands against a copy of an existing OpenClaw lcm.db and diff the output.

2. **`api.runtime.agent.session.resolveSessionFilePath` semantics.** TS plugin uses this to discover all configured agents' session-store paths at startup (`listStartupSessionFileCandidates`). The port needs a Hermes equivalent — `gateway/session.py` has the session-store, but the API to enumerate "all agents with active sessions" may not exist as-is. **Mitigation:** check `hermes_state.py` and `gateway/session.py`; if missing, file a follow-up to expose `iter_active_session_files()`.

3. **Prompt cache regression risk** (system vs user injection). OpenClaw prepends the recall policy to the SYSTEM PROMPT; Hermes deliberately injects to USER MESSAGES to preserve prompt cache. If the port follows Hermes convention strictly, every turn ships ~3 KB of user-message preamble which adds latency. **Mitigation:** measure with `claude-api`'s cache-hit rate harness; if regression is real, propose `pre_system_prompt_build` hook to Hermes core.

4. **Owner-gating defense-in-depth.** Relying purely on `gateway/slash_access` means a misconfigured `allow_admin_from` (empty / unset) leaves `/lcm purge` open to any DM-allowed user. Operators may not know to set this when installing the port. **Mitigation:** add a startup check that warns if LCM is loaded but `allow_admin_from` is unset on any platform; add the warning to the welcome banner.

5. **OpenClaw `register_tool` per-ctx factory pattern.** TS `api.registerTool((ctx) => createLcmGrepTool({ ..., sessionKey: ctx.sessionKey, getRuntimeContext: () => getTokenStateRuntimeContext(ctx.sessionKey) }))` creates a NEW tool instance per call, closing over the call's session key. Hermes `ctx.register_tool(name, toolset, schema, handler, ...)` registers ONE handler globally; per-call session key must come from a Hermes thread-local or be passed via the handler kwargs. **Mitigation:** verify Hermes `tools/registry.py` passes the session key to handlers (likely via the `task_id` / `session_id` kwargs already in `invoke_hook("pre_tool_call", ...)`); if not, the engine must maintain a thread-local session id.
