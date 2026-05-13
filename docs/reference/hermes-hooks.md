# Hermes Hook & Plugin API Reference

**For:** lossless-hermes port architecture
**Hermes version:** `main` as of 2026-05-13 (read from `/Volumes/LEXAR/Claude/hermes-agent`)
**Confidence target:** 95% (evidence-based; every signature, kwarg, and dispatch site below is grounded in a file:line citation)

---

## TL;DR — what a Hermes plugin can actually do

A Hermes plugin is a Python package (or in-repo directory) shipped with a `plugin.yaml` manifest and an `__init__.py` defining `register(ctx)`. Inside `register`, the plugin calls methods on a `PluginContext` (`ctx`) to register one or more of:

- **A context engine** (replaces `ContextCompressor`; only one plugin wins; lossless-hermes uses this).
- **Tools** (LLM-callable functions registered in the global tool registry).
- **Slash commands** (`/lcm ...`, runs in CLI/TUI/gateway sessions).
- **CLI subcommands** (`hermes <subcommand>`, terminal-only).
- **Hooks** (lifecycle callbacks: `pre_llm_call`, `post_llm_call`, `subagent_stop`, etc.).
- **Skills** (read-only namespaced SKILL.md docs).
- **Image-gen providers, gateway platform adapters** (out of scope for LCM).

Memory providers go through a **separate** discovery path (`plugins/memory/__init__.py`), not the general `PluginContext`. See "Where LCM hooks land" below.

---

## ContextEngine ABC — complete surface

File: `/Volumes/LEXAR/Claude/hermes-agent/agent/context_engine.py` (206 lines).

### Required class-level state (read by `run_agent.py` directly)

These are class attributes the engine **must maintain** — `run_agent.py` reads them by direct attribute access for display/logging:

| Attribute | Default | Read at | Purpose |
|---|---|---|---|
| `last_prompt_tokens` | `0` | `run_agent.py:14825,14831,14845` | Real token count from last API response; used to decide compress |
| `last_completion_tokens` | `0` | tracked in `update_from_response` | display |
| `last_total_tokens` | `0` | display | display |
| `threshold_tokens` | `0` | `run_agent.py:11971,15461` | When `last_prompt_tokens >= this`, fire compress |
| `context_length` | `0` | `run_agent.py:2330,2374,2444` | Total model context window |
| `compression_count` | `0` | `run_agent.py:10377` | Display counter |
| `threshold_percent` | `0.75` | `agent/context_engine.py:59` | Preflight: at what % of context_length to fire compress |
| `protect_first_n` | `3` | `run_agent.py:11960` | Messages from start never compressed |
| `protect_last_n` | `6` | `run_agent.py:11961` | Messages at end never compressed |

### Methods

| Method | Required? | Signature | When called | Return semantics |
|---|---|---|---|---|
| `name` (property) | **yes** | `→ str` | Identity; used by `run_agent.py:2277,2309` for "Using context engine: <name>" | Short identifier (e.g. `"compressor"`, `"lcm"`) |
| `update_from_response` | **yes** | `(usage: Dict[str, Any]) → None` | `run_agent.py:13046` — after every successful LLM API call. `usage` dict has `prompt_tokens`, `completion_tokens`, `total_tokens` keys. | Mutate self (update `last_*_tokens`). Return ignored. |
| `should_compress` | **yes** | `(prompt_tokens: int = None) → bool` | `run_agent.py:14841` — after each turn's API call. Called with real prompt tokens. | `True` → fires `_compress_context`, which calls `compress(messages, current_tokens=..., focus_topic=...)`. |
| `compress` | **yes** | `(messages: List[Dict], current_tokens: int = None, focus_topic: str = None) → List[Dict]` | `run_agent.py:10264,10268` — when `should_compress()` returns True, or on preflight (line 11971), or via `/compress <focus>` slash command (`gateway/run.py:10774`). | Returns the new (shorter) message list. Must be a valid OpenAI-format message sequence. Engines that don't support `focus_topic` may receive a fallback call without it (TypeError-recovery at run_agent.py:10265-10268). |
| `should_compress_preflight` | no | `(messages: List[Dict]) → bool` | **NOT CALLED IN CURRENT CODEBASE.** Declared at `agent/context_engine.py:100`, but `grep` finds zero call sites in non-test code as of 2026-05-13. Tests confirm default returns `False`. Safe to ignore. | (unused) |
| `has_content_to_compress` | no | `(messages: List[Dict]) → bool` | `gateway/run.py:10768` — manual `/compress` preflight guard. Returns "nothing to compress yet" message if False. | Default returns `True`. |
| `on_session_start` | no | `(session_id: str, **kwargs) → None` | `run_agent.py:2369` — once per `AIAgent.__init__` after engine is bound. **kwargs always include** `hermes_home`, `platform`, `model`, `context_length`. | Side effects (load DAG/store/etc.). Return ignored. |
| `on_session_end` | no | `(session_id: str, messages: List[Dict]) → None` | `run_agent.py:5575,5600` — at real session boundaries: `shutdown_memory_provider()`, `commit_memory_session()` (which fires on `/new`/`/reset` and at process exit). NOT per-turn. | Side effects (flush state, close DB). Return ignored. |
| `on_session_reset` | no | `() → None` | `run_agent.py:2563` — only in `AIAgent.reset_session()`. Default implementation zeroes `last_*_tokens` and `compression_count`. | Reset per-session state. Return ignored. |
| `get_tool_schemas` | no | `() → List[Dict]` | `run_agent.py:2355` — once at engine bind, injects each schema (wrapped as `{"type": "function", "function": <schema>}`) into the agent's tool list AND records each schema's `"name"` in `self._context_engine_tool_names`. | Schemas in OpenAI function format. Each must have a `"name"` key at top level (not nested inside `"function"`). |
| `handle_tool_call` | no | `(name: str, args: Dict, **kwargs) → str` | `run_agent.py:11249` — when the LLM calls one of the names registered via `get_tool_schemas`. **kwargs include** `messages` (current in-memory message list). | **Must return a JSON string.** Errors raise `Exception` → caller wraps in `{"error": "..."}`. |
| `get_status` | no | `() → Dict[str, Any]` | Optional display hook. Default returns `last_prompt_tokens`, `threshold_tokens`, `context_length`, `usage_percent`, `compression_count`. | Status dict. |
| `update_model` | no | `(model: str, context_length: int, base_url: str = "", api_key: str = "", provider: str = "") → None` | `run_agent.py:2301,2728,8811` — on init, model switch, and fallback activation. | Mutate self. Default sets `context_length` + recalculates `threshold_tokens = context_length * threshold_percent`. |

### Plugin loader for context engines

File: `/Volumes/LEXAR/Claude/hermes-agent/plugins/context_engine/__init__.py`.

**Two discovery paths for context engines:**

1. **In-repo, under `plugins/context_engine/<name>/`** — `discover_context_engines()` scans this dir; `load_context_engine(name)` returns an instance. This is the path the built-in `compressor` and any future repo-shipped LCM would take.
2. **General plugin system** — a plugin (anywhere) calls `ctx.register_context_engine(engine)`; `get_plugin_context_engine()` retrieves it. **lossless-hermes will use this path.**

Both paths are consulted in `run_agent.py:2256-2287`. Selection is driven by `context.engine` in `config.yaml` (default `"compressor"`).

**Only ONE engine wins.** `PluginContext.register_context_engine()` rejects subsequent calls with a warning (`hermes_cli/plugins.py:496-502`).

---

## VALID_HOOKS — every hook, every kwarg, every dispatch site

Source: `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:128-168`.

The constant `VALID_HOOKS` is the canonical list. Hooks are invoked via `invoke_hook(name, **kwargs)`; each callback runs inside a try/except so a misbehaving plugin can't break the agent loop (`plugins.py:1218-1232`). Return values are accumulated and returned as a list to the caller; the caller decides how to use them.

| Hook | Fired at | kwargs | Return-value semantics |
|---|---|---|---|
| `pre_tool_call` | `model_tools.py:743` (via `get_pre_tool_call_block_message`); also `run_agent.py:10508,10672,11054` for the concurrent path. Fires **before** each tool dispatch, exactly once per call. | `tool_name: str`, `args: dict`, `task_id: str`, `session_id: str`, `tool_call_id: str` | Plugins may return `{"action": "block", "message": "..."}` to abort the tool. First valid block wins; all other returns are ignored. Otherwise observer-only. |
| `post_tool_call` | `model_tools.py:793`. Fires **after** every tool dispatch, observer-only. | `tool_name: str`, `args: dict`, `result: str` (the JSON string), `task_id: str`, `session_id: str`, `tool_call_id: str`, `duration_ms: int` | Return values ignored. |
| `transform_tool_result` | `model_tools.py:814`. Fires **after** `post_tool_call`, **before** result enters conversation. | Same as `post_tool_call`. | First plugin to return a `str` wins and replaces `result`. Non-string returns ignored. |
| `transform_terminal_output` | `tools/terminal_tool.py:2057`. Fires inside the `terminal` tool, **before** truncation/redaction. | `command: str`, `output: str`, `returncode: int`, `task_id: str`, `env_type: str` | First plugin to return a `str` wins. |
| `transform_llm_output` | `run_agent.py:15389`. Fires once per turn **after** the tool-calling loop, before `post_llm_call` and before the final response is returned. | `response_text: str`, `session_id: str`, `model: str`, `platform: str` | First non-empty `str` wins (replaces `final_response`). |
| `pre_llm_call` | `run_agent.py:12034`. Fires **once per user turn**, before the tool-calling loop. | `session_id: str`, `user_message: <Any>` (the raw original user msg), `conversation_history: list` (copy of `messages`), `is_first_turn: bool`, `model: str`, `platform: str`, `sender_id: str` (gateway platform user id; empty in CLI) | Plugins may return `{"context": "<recalled text>"}` or a plain `str`. All non-None returns are concatenated with `\n\n` and **appended to the current turn's user message** (NOT injected into the system prompt — preserves Anthropic prompt cache). Ephemeral; not persisted to SQLite. |
| `post_llm_call` | `run_agent.py:15410`. Fires **once per user turn** at the end of `run_conversation`, AFTER `transform_llm_output`, only when `final_response` is set AND `not interrupted`. | `session_id: str`, `user_message`, `assistant_response: str`, `conversation_history: list` (the final post-tool-loop messages), `model: str`, `platform: str` | Observer; returns ignored. **This is where LCM `ingest()` lives.** |
| `pre_api_request` | `run_agent.py:12509`. Fires **before each individual API call** (so multiple times per turn if tool calls cause iteration). | `task_id: str`, `session_id: str`, `platform: str`, `model: str`, `provider: str`, `base_url: str`, `api_mode: str`, `api_call_count: int`, `message_count: int`, `tool_count: int`, `approx_input_tokens: int`, `request_char_count: int`, `max_tokens: int` | Observer; returns ignored. |
| `post_api_request` | `run_agent.py:14404`. Fires **after each individual API call**, observer-only. | All `pre_api_request` kwargs plus `api_duration: float`, `finish_reason: str`, `response_model: str`, `usage: dict`, `assistant_content_chars: int`, `assistant_tool_call_count: int` | Observer; returns ignored. |
| `on_session_start` | `run_agent.py:11933`. Fires **once when a brand-new session is created** (NOT on continuation; only when `stored_prompt` is empty). | `session_id: str`, `model: str`, `platform: str` | Observer; returns ignored. **NOT** the same as `ContextEngine.on_session_start` — that one fires unconditionally on every `AIAgent.__init__` (line 2369). |
| `on_session_end` | `run_agent.py:15525`. Fires **at the end of EVERY `run_conversation` call** (i.e., every user turn), plus safety-net fire on interrupted CLI exit (`cli.py:13233`). Per-turn cadence. | `session_id: str`, `completed: bool`, `interrupted: bool`, `model: str`, `platform: str` | Observer; returns ignored. **NOT** the same as `ContextEngine.on_session_end` — that one fires at real session boundaries (`shutdown_memory_provider`, `commit_memory_session`). |
| `on_session_finalize` | `cli.py:728` (CLI exit), `gateway/run.py:8155` (`/new`), `gateway/run.py:3983` (gateway session expiry), `gateway/run.py:2855` (gateway shutdown), `tui_gateway/server.py:280` (TUI close). True session boundary. | `session_id: <Any>`, `platform: str` | Observer; returns ignored. |
| `on_session_reset` | `gateway/run.py:8225` (after `/new` creates the new session), `cli.py:5530` (CLI `/new`). | `session_id: str`, `platform: str` | Observer; returns ignored. |
| `subagent_stop` | `tools/delegate_tool.py:2248`. Fires once **per child** after `delegate_task` runs, serialised on the parent thread. | `parent_session_id: str`, `child_role: <Any>`, `child_summary: str`, `child_status: str`, `duration_ms: int` | Observer; returns ignored. |
| `pre_gateway_dispatch` | `gateway/run.py:5680`. Fires for **every incoming user message** after the internal-event guard but BEFORE auth/pairing. Gateway-only. | `event: MessageEvent`, `gateway: GatewayRunner`, `session_store: SessionStore` | May return `{"action": "skip", "reason": "..."}` (drop), `{"action": "rewrite", "text": "..."}` (mutate event.text and continue), or `{"action": "allow"}` / `None` (normal dispatch). First non-None action wins (`gateway/run.py:5690-5709`). |
| `pre_approval_request` | `tools/approval.py:1185` (gateway path), `tools/approval.py:1322` (CLI path). Fires when a dangerous command needs user approval. | `command: str`, `description: str`, `pattern_key: str`, `pattern_keys: list[str]`, `session_key: str`, `surface: "cli" \| "gateway"` | Observer only — return values are **explicitly** ignored. Cannot veto here; use `pre_tool_call` to block. |
| `post_approval_response` | `tools/approval.py:1267,1334`. Fires after the user responds (or timeout). | Same as `pre_approval_request` plus `choice: "once" \| "session" \| "always" \| "deny" \| "timeout"` | Observer; ignored. |

### Important: there is no `compress_started` / `compress_finished` hook

Compression is invoked directly on `self.context_compressor.compress(...)` (`run_agent.py:10264,10268`). No plugin hook fires around compression — the engine itself is the integration point. LCM, as the engine, owns the entire compression lifecycle without needing a hook.

---

## PluginContext — the surface a plugin sees

File: `hermes_cli/plugins.py:287-665`. The `ctx` object passed to `register(ctx)`.

### Attributes

| Attribute | Purpose | Source |
|---|---|---|
| `ctx.manifest: PluginManifest` | The parsed `plugin.yaml`. Fields: `name`, `version`, `description`, `author`, `requires_env`, `provides_tools`, `provides_hooks`, `source` (`"bundled"\|"user"\|"project"\|"entrypoint"`), `path`, `kind`, `key` | `plugins.py:233-267,291` |
| `ctx.llm` (property) | Lazy-built `PluginLlm` facade (`agent/plugin_llm.py`). Lets trusted plugins call the host's active model without their own API keys. Override capability is fail-closed (provider/model/agent/profile overrides all gated through `plugins.entries.<id>.llm.*` in config.yaml). | `plugins.py:298-313` |
| **`ctx.hermes_home`** | **Does NOT exist.** Plugins call `from hermes_constants import get_hermes_home` directly. | (absent in `plugins.py`) |
| **`ctx.plugin_config`** | **Does NOT exist.** Plugins read config themselves via `from hermes_cli.config import load_config; cfg_get(load_config(), "plugins", "<plugin-name>", ...)`. Example: `plugins/memory/holographic/__init__.py:97-108`. | (absent in `plugins.py`) |
| **`ctx.is_owner`** | **Does NOT exist.** No ownership/admin gate is surfaced through `ctx`. | (absent in `plugins.py`) |
| `ctx._manager` | Backref to `PluginManager` (private). Plugins should not use this. | `plugins.py:292` |

### Methods (`register_*` + helpers)

| Method | Signature | Purpose / behaviour |
|---|---|---|
| `register_context_engine(engine)` | `engine: ContextEngine` — **one positional arg**, NOT `(name, engine)`. The engine's `.name` property is used as identity. | `plugins.py:488`. Only one wins; second call rejected with warning. `engine` must `isinstance(ContextEngine)` (defensive check at line 504). |
| `register_tool(name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False, description="", emoji="")` | Note `toolset` is **positional arg #2** and required. | `plugins.py:317-344`. Delegates to `tools.registry.register()`. Tool then appears in the LLM's tool list alongside built-ins. Plugin tools are tracked in `_plugin_tool_names`. |
| `register_command(name, handler, description="", args_hint="")` | `handler: Callable[[str], str \| None]` (may be async). | `plugins.py:401-453`. **Slash command** (e.g. `/lcm`) available in CLI, TUI, and gateway sessions. Name is normalized: lowercased, stripped, leading `/` removed, spaces → `-`. Conflicts with built-ins are rejected. `args_hint` is surfaced by Discord adapter for native slash-command picker. |
| `register_cli_command(name, help, setup_fn, handler_fn=None, description="")` | `setup_fn` receives an `argparse` subparser. | `plugins.py:376-397`. **Terminal subcommand** (`hermes <name> ...`). Independent of slash commands — does NOT auto-register the same name as `/foo`. |
| `register_hook(hook_name, callback)` | `callback: Callable[..., Any]`. | `plugins.py:603-618`. Unknown hook names produce a warning but are still stored (forward-compat). |
| `register_skill(name, path, description="")` | `path: pathlib.Path` to a SKILL.md file. | `plugins.py:622-665`. Skill becomes resolvable as `'<plugin_name>:<name>'`. NOT listed in the system prompt's `<available_skills>` — opt-in explicit loads only. Name must match `[a-zA-Z0-9_-]+`, must not contain `:`. |
| `register_image_gen_provider(provider)` | `provider: ImageGenProvider` instance. | `plugins.py:520-543`. Routes `image_generate` tool calls. Out of scope for LCM. |
| `register_platform(name, label, adapter_factory, check_fn, validate_config=None, required_env=None, install_hint="", **entry_kwargs)` | Gateway messaging platform adapter (IRC, Slack, etc.). | `plugins.py:547-599`. Out of scope for LCM. |
| `inject_message(content: str, role: str = "user") → bool` | Inject a message into the active CLI conversation. | `plugins.py:348-372`. **CLI-only** (returns False if `_cli_ref is None`, e.g. in gateway mode). If agent is idle, queues as next input; if mid-turn, interrupts. Not relevant to LCM (which interacts via hooks/tools, not message injection). |
| `dispatch_tool(tool_name, args, **kwargs) → str` | Dispatch a tool call through the registry, with parent-agent context injected. | `plugins.py:457-484`. Used by plugin slash commands that want to call `delegate_task` etc. without reaching into framework internals. |

### What is NOT on `PluginContext`

These appear in the user's draft outline but **do not exist**:

- `ctx.hermes_home` — plugins call `get_hermes_home()` directly.
- `ctx.plugin_config` — plugins read their own slice of `config.yaml` via `cfg_get`.
- `ctx.is_owner` — no admin/ownership gate is exposed.
- `register_memory_provider` — memory has its OWN discovery path (`plugins/memory/__init__.py`); a fake `_ProviderCollector` mocks the call. LCM is NOT a memory provider — it's a context engine.

---

## Slash command vs CLI command — the difference

| | `register_command` | `register_cli_command` |
|---|---|---|
| Invocation | `/lcm <args>` inside a CLI/TUI/gateway session | `hermes <subcommand> ...` from the shell |
| Lives in | `_plugin_commands` dict | `_cli_commands` dict |
| Handler signature | `fn(raw_args: str) → str \| None` (sync or async) | `setup_fn(subparser)` + optional `handler_fn` (argparse-based) |
| Available in gateway? | Yes (CLI, TUI, gateway all dispatch via `get_plugin_command_handler`) | No — terminal only |
| Use case | In-conversation actions (e.g. `/lcm focus quantum-physics`) | One-shot terminal ops (e.g. `hermes lcm-doctor`) |

**Both can coexist** for the same plugin. lossless-hermes will register `/lcm` (slash) for in-session subcommand dispatch, and possibly `hermes lcm-doctor-*` (CLI) for offline diagnostics.

Slash-command dispatch sites:
- CLI: `cli.py:7635` calls `get_plugin_command_handler(base_cmd.lstrip("/"))`.
- TUI: `tui_gateway/server.py:4486,5494`.
- Gateway: dispatched via the same `_plugin_commands` dict; `commands.py:613` enumerates them.

Async handlers are awaited by `resolve_plugin_command_result()` (`plugins.py:1378-1421`) with a 30s timeout.

---

## Tool registration — `register_tool` vs `ContextEngine.get_tool_schemas`

| | `ctx.register_tool(...)` | `ContextEngine.get_tool_schemas()` |
|---|---|---|
| Registered in | `tools.registry.registry` (global) | `AIAgent.tools` list (per-agent, on init) and `self._context_engine_tool_names` (per-agent set) |
| Dispatched by | `tools.registry.dispatch(name, args, ...)` — same code path as built-ins | `context_compressor.handle_tool_call(name, args, messages=messages)` (`run_agent.py:11249`) |
| Schema format | `{name: str, description, parameters: {...}}` (passed to `registry.register`); description and emoji separate kwargs | OpenAI function-call schema (with `name` at top level); wrapped in `{"type": "function", "function": schema}` before injection |
| `pre_tool_call` / `post_tool_call` fires? | **Yes** — same registry path, same hook checks | **No** — separate dispatch branch; the registry hook checks are bypassed |
| Best fit for LCM | Single utilities you want available everywhere | The 8 LCM agent tools (`lcm_grep`, `lcm_describe`, `lcm_expand`, etc.) that need access to the engine's state |

**Recommendation for lossless-hermes:** put the 8 LCM agent tools on `get_tool_schemas` / `handle_tool_call` so they share state with the engine instance. If you also want the tools to be observable by `post_tool_call` plugins (audit logs, etc.), you'd need to either (a) wrap the handle in a manual `invoke_hook("post_tool_call", ...)` or (b) ALSO register them via `ctx.register_tool`. The former is cheaper; the latter risks duplicate-name errors in the OpenAI schema (the dedup at `run_agent.py:2354-2358` exists exactly for that case but only suppresses second-registration, not double-firing).

---

## Subagent / `delegate_task` lifecycle — how a plugin observes subagents

`delegate_task` is the agent tool that spawns child `AIAgent`s. The relevant lifecycle for plugins is:

1. Parent calls `delegate_task` (registry tool, fires `pre_tool_call` once).
2. Each child runs its own full `run_conversation` — meaning **its own** `pre_llm_call`, `post_llm_call`, `pre_api_request`, `post_api_request`, `on_session_start` (if a new session), and `on_session_end` (per turn) hooks fire with the child's `session_id`. The child's `_user_id` is inherited from parent if available (`run_agent.py:2018-2019`).
3. After **all** children finish, the parent thread fires `subagent_stop` **once per child** (`tools/delegate_tool.py:2248`) with `parent_session_id`, `child_role`, `child_summary`, `child_status`, `duration_ms`. Serialised on the parent thread so plugin authors don't have to handle concurrency.
4. The `post_tool_call` for `delegate_task` itself fires at the parent's level with the aggregated JSON `result`.

**Implication for LCM:** to track subagent traces, hook `subagent_stop` and read `child_summary`. If the goal is to ingest each child's full transcript, attach to `post_llm_call` (which fires per-child-turn with the child's `session_id`) and discriminate by `session_id`.

---

## Config delivery — where does plugin config come from?

**There is no automatic `ctx.plugin_config` injection.** Plugins read their own config slice from `~/.hermes/config.yaml`:

```python
# Canonical pattern (plugins/memory/holographic/__init__.py:97-108)
from hermes_cli.config import load_config, cfg_get
from hermes_constants import get_hermes_home

config = load_config() or {}
my_config = cfg_get(config, "plugins", "<plugin-name>", default={}) or {}
```

Common config keys for a plugin named `<X>`:
- `plugins.enabled: [<X>, ...]` — opt-in allowlist (mandatory; without this, plugin won't load even if installed).
- `plugins.disabled: [<X>, ...]` — explicit deny list (overrides `enabled`).
- `plugins.entries.<X>.<anything>` — convention used by the LLM-trust system (`agent/plugin_llm.py:202-246`); plugins are encouraged to put their settings here.
- `plugins.<X>.<anything>` — used by `holographic` memory plugin. No formal convention is enforced.

**For lossless-hermes:** read config in `register(ctx)` from `cfg_get(load_config(), "plugins", "entries", "lossless-hermes", default={})`. Pass it to the `LCMEngine` constructor. Document the config schema in your README.

---

## `context.engine` selection

In `~/.hermes/config.yaml`:

```yaml
context:
  engine: lcm     # default: "compressor"
```

Resolution order (`run_agent.py:2256-2287`):
1. If `engine == "compressor"`, use built-in `ContextCompressor` — **plugin engines are NOT consulted even if registered.** This is the safety default.
2. Try `plugins/context_engine/<name>/` directory (repo-shipped path).
3. Try general plugin system: `get_plugin_context_engine()` returns the engine ONLY IF its `.name` property matches `<engine_name>`. **lossless-hermes must set `engine.name = "lcm"` to be selectable.**
4. Otherwise warn and fall back to compressor.

The agent calls `engine.update_model(...)` then `engine.on_session_start(...)` at init, then injects `get_tool_schemas()` into the tool list (`run_agent.py:2289-2375`).

---

## Plugin entry-point shape (recommended for lossless-hermes)

### `pyproject.toml`

```toml
[project]
name = "lossless-hermes"
version = "0.1.0"

[project.entry-points."hermes_agent.plugins"]
lossless-hermes = "lossless_hermes:register"
```

Entry-point group: **`hermes_agent.plugins`** (verified at `hermes_cli/plugins.py:170`).

### `src/lossless_hermes/__init__.py`

```python
from hermes_cli.config import load_config, cfg_get
from hermes_constants import get_hermes_home
from .engine import LCMEngine
from .commands import lcm_command_dispatcher

def register(ctx):
    config = cfg_get(load_config(), "plugins", "entries", "lossless-hermes", default={})
    engine = LCMEngine(
        hermes_home=get_hermes_home(),
        config=config,
    )
    # Context engine slot — only one wins, must be selected via context.engine: lcm
    ctx.register_context_engine(engine)
    # post_llm_call: LCM's ingest() — captures the full conversation_history after each turn.
    ctx.register_hook("post_llm_call", engine._on_post_llm_call)
    # pre_llm_call: LCM's assemble() — append recalled context to the user message.
    # Must return {"context": "..."} or a string.
    ctx.register_hook("pre_llm_call", engine._on_pre_llm_call)
    # Slash-command dispatch for /lcm <sub> (25 subcommands → one handler).
    ctx.register_command(
        name="lcm",
        handler=lcm_command_dispatcher,
        description="LCM subsystem control (focus, doctor, recall, ...)",
        args_hint="<subcommand>",
    )
```

### `plugin.yaml` (only needed if shipped in-repo or in `~/.hermes/plugins/`)

```yaml
name: lossless-hermes
version: 0.1.0
description: "Lossless context management (LCM) engine for Hermes — replaces ContextCompressor."
author: "..."
kind: standalone   # NOT exclusive (that's memory-provider-only)
hooks:
  - pre_llm_call
  - post_llm_call
provides_tools:
  - lcm_grep
  - lcm_describe
  - lcm_expand
  # (etc.)
```

Pip-installed plugins do NOT need `plugin.yaml` — they go through the entry-point path (`plugins.py:1039-1063`). Local `~/.hermes/plugins/lossless-hermes/` directory installs DO need `plugin.yaml`.

### `config.yaml` (user must set both)

```yaml
context:
  engine: lcm

plugins:
  enabled:
    - lossless-hermes
  entries:
    lossless-hermes:
      # ... your plugin-specific config keys ...
```

---

## Where LCM hooks land

| LCM concept | Best-fit Hermes hook | Notes |
|---|---|---|
| `bootstrap(sessionFile)` | `ContextEngine.on_session_start(session_id, hermes_home=..., platform=..., model=..., context_length=...)` | Fires unconditionally at `AIAgent.__init__` (`run_agent.py:2369`). `hermes_home` is in kwargs — use it to locate the session file. |
| `ingest(message)` | `register_hook("post_llm_call", ...)` | Per-turn; receives `session_id`, `user_message`, `assistant_response`, `conversation_history` (post-tool-loop snapshot), `model`, `platform`. Diff against the prior snapshot to identify new turns. |
| Always-on `assemble()` substitution | `register_hook("pre_llm_call", ...)` | Plugins return `{"context": "..."}` or a plain string; result is **appended to the user message**, NOT injected into the system prompt (`plugins.py:1206-1216`). System prompt is reserved for Hermes (preserves prompt cache). If you need to *replace* messages mid-flight (not just append), the only mechanism is `ContextEngine.compress()` — i.e., force `should_compress() → True`. |
| `compact(...)` | `ContextEngine.compress(messages, current_tokens, focus_topic)` | Direct mapping. Called by `_compress_context` (`run_agent.py:10264`) when `should_compress()` returns True. |
| `afterTurn(...)` | `register_hook("post_llm_call", ...)` | Same hook as `ingest` — if you have logically distinct concerns, run both inside the single callback. |
| `maintain(...)` background work | Spawn an asyncio task in `ContextEngine.on_session_start` (or in `register()`). Guard with a PID lock. Tear down in `on_session_end`. | No first-class background-worker API. Plugins create their own thread/task. |
| 8 LCM agent tools | `ContextEngine.get_tool_schemas()` + `handle_tool_call()` | Direct path. Schema list returned at init is injected into `AIAgent.tools` (`run_agent.py:2355`); calls dispatched via `handle_tool_call(name, args, messages=...)` (`run_agent.py:11249`). |
| `/lcm <subcommand>` (25 subcommands) | `ctx.register_command("lcm", lcm_dispatcher, args_hint="<subcommand>")` | ONE top-level slash command; your dispatcher parses `raw_args` and routes to the 25 subcommand handlers. Available in CLI, TUI, gateway. |
| Doctor commands | TWO options: (a) include them as `/lcm doctor <kind>` subcommands of the slash dispatcher, (b) register each as a `hermes lcm-doctor-<kind>` CLI command via `register_cli_command`. **Recommendation:** (a) for parity across CLI/gateway; reserve (b) for ops scripts that must run without a session loaded. | See "Slash vs CLI" table above. |

---

## Open questions (need resolution before LCM ports)

1. **Mutable-message rewrite for `pre_llm_call`:** Hermes's contract is "context is *appended* to the user message; system prompt is read-only." LCM's `assemble()` historically expected to *substitute* selected messages. If LCM needs full rewrite, the only path is via `compress()` (which **replaces** the message list). Decide whether `pre_llm_call` append-only is sufficient, or whether LCM must force `should_compress() → True` every turn to gain rewrite authority. (Tracked as "Spike 002" in the user's draft.)
2. **Tool-name collisions:** `register_tool` and `get_tool_schemas` both register names into the same agent's tool list with a dedup pass (`run_agent.py:2354-2358`). Confirm the 8 LCM tool names don't collide with any built-in or other-plugin tool (`grep` is taken? no — built-in is `search_files`; LCM's `lcm_grep` is clear).
3. **`post_llm_call` does NOT fire when interrupted:** `run_agent.py:15407` gates on `if final_response and not interrupted`. If the user `Ctrl-C`s mid-turn, LCM's `ingest()` will not run for that turn. The fallback CLI hook at `cli.py:13233` is `on_session_end`, NOT `post_llm_call`. LCM may need to also hook `on_session_end` to flush a partial-turn buffer.
4. **`is_first_turn` vs continuation:** `pre_llm_call` carries `is_first_turn: bool`, but `on_session_start` (plugin hook, not the ContextEngine method) fires only when `stored_prompt` is empty — i.e. on a brand-new session, not when continuing one. LCM's bootstrap probably wants the ContextEngine.on_session_start method (unconditional), not the plugin hook.
5. **`config.yaml` schema:** decide concrete keys under `plugins.entries.lossless-hermes.*` and document them — there is no Hermes-side schema validation.

---

## Remaining 5% risk

- **Hermes upstream renames a hook.** `VALID_HOOKS` is a hard-coded set; renaming would require LCM to ship a compat shim. Mitigation: pin Hermes version in lossless-hermes integration tests; subscribe to Hermes release notes for hook-API changes.
- **Hermes splits `post_llm_call` kwargs.** Currently `conversation_history` is `list(messages)` — a full copy. If Hermes ever changes this to a view or a delta for perf, LCM's ingest logic that does full-snapshot diffing breaks.
- **Hermes gates `register_context_engine` further.** Today the only gate is "only one engine wins." Hermes could add a `trusted_engines` allowlist (similar to the `plugins.entries.<id>.llm.*` trust scheme for `PluginLlm`). Mitigation: keep the engine's runtime behaviour explainable and conservative; don't over-claim provider/model overrides.
- **`should_compress_preflight` becomes load-bearing.** It's declared but unreached today. If a future Hermes version starts calling it for cheaper preflight compression checks, LCM's default `False` could cause skipped compression. Mitigation: implement `should_compress_preflight` to return True iff the engine would benefit (cheap to add).
- **`focus_topic` kwarg to `compress`.** Plugin engines with strict signatures that don't accept `focus_topic` fall back via `TypeError` catch (`run_agent.py:10265-10268`). Don't rely on `focus_topic` always being passed; treat it as optional.
- **The plugin discovery order assumes `plugins.enabled` is set.** If the user upgrades from a pre-opt-in Hermes, the migration `migrate_config` populates a grandfathered set. lossless-hermes won't be in that grandfathered set (it's new), so installation docs MUST instruct the user to add `lossless-hermes` to `plugins.enabled`.

---

## Appendix: file:line cross-reference

| Topic | File:Line |
|---|---|
| `ContextEngine` ABC | `/Volumes/LEXAR/Claude/hermes-agent/agent/context_engine.py:32-207` |
| In-repo context engine loader | `/Volumes/LEXAR/Claude/hermes-agent/plugins/context_engine/__init__.py:79-220` |
| `VALID_HOOKS` constant | `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:128-168` |
| `PluginContext` class | `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:287-665` |
| `PluginManager.invoke_hook` | `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:1198-1232` |
| `get_pre_tool_call_block_message` | `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:1315-1351` |
| Context engine selection | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:2251-2310` |
| Engine tool-schema injection | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:2340-2364` |
| `ContextEngine.on_session_start` fire | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:2369-2375` |
| `ContextEngine.handle_tool_call` fire | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:11249` |
| `pre_llm_call` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:12033-12053` |
| `post_llm_call` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:15408-15420` |
| `pre_api_request` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:12507-12526` |
| `post_api_request` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:14400-14423` |
| `transform_llm_output` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:15387-15401` |
| `on_session_start` (plugin hook) dispatch | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:11931-11940` |
| `on_session_end` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:15523-15534` |
| `on_session_finalize` dispatch (CLI) | `/Volumes/LEXAR/Claude/hermes-agent/cli.py:727-730` |
| `on_session_finalize` dispatch (gateway shutdown) | `/Volumes/LEXAR/Claude/hermes-agent/gateway/run.py:2853-2858` |
| `on_session_finalize` dispatch (gateway expiry) | `/Volumes/LEXAR/Claude/hermes-agent/gateway/run.py:3979-3988` |
| `on_session_finalize` dispatch (gateway /new) | `/Volumes/LEXAR/Claude/hermes-agent/gateway/run.py:8153-8158` |
| `on_session_reset` dispatch (gateway) | `/Volumes/LEXAR/Claude/hermes-agent/gateway/run.py:8223-8228` |
| `on_session_reset` dispatch (CLI) | `/Volumes/LEXAR/Claude/hermes-agent/cli.py:5528-5536` |
| `pre_tool_call` dispatch (registry path) | `/Volumes/LEXAR/Claude/hermes-agent/model_tools.py:743-755` |
| `post_tool_call` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/model_tools.py:792-804` |
| `transform_tool_result` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/model_tools.py:812-829` |
| `transform_terminal_output` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/tools/terminal_tool.py:2055-2070` |
| `subagent_stop` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/tools/delegate_tool.py:2244-2257` |
| `pre_gateway_dispatch` dispatch | `/Volumes/LEXAR/Claude/hermes-agent/gateway/run.py:5677-5709` |
| `pre_approval_request` / `post_approval_response` fire | `/Volumes/LEXAR/Claude/hermes-agent/tools/approval.py:1185-1276, 1322-1343` |
| Compression invocation | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:10264-10268` |
| `update_from_response` invocation | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:13046` |
| `should_compress` invocation | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:14841` |
| `has_content_to_compress` invocation | `/Volumes/LEXAR/Claude/hermes-agent/gateway/run.py:10768` |
| `ContextEngine.on_session_end` invocation | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:5575,5600` |
| `ContextEngine.on_session_reset` invocation | `/Volumes/LEXAR/Claude/hermes-agent/run_agent.py:2563` |
| Entry-point group name | `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:170` |
| Plugin discovery (4 sources) | `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:692-857` |
| Synthetic hook payloads for `hermes hooks test` | `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/hooks.py:112-185` |
