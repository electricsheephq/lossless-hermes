# ADR-001: Plugin distribution model — entry-point vs directory-mode

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

Hermes discovers plugins through four channels (`hermes_cli/plugins.py:692-857`). Two of them are operationally relevant to lossless-hermes:

1. **Entry-point mode** — a pip-installed package declares `[project.entry-points."hermes_agent.plugins"]` and Hermes iterates `importlib.metadata.entry_points(group="hermes_agent.plugins")` at startup (`hermes_cli/plugins.py:170`, `:1039-1063`). The package's `register(ctx)` callable runs with a full `PluginContext`.
2. **Directory mode** — the operator drops a directory under `~/.hermes/plugins/<name>/` containing `plugin.yaml` and a Python module. Loading goes through `_EngineCollector` (`plugins/context_engine/__init__.py:199-219`) for context engines.

Lossless-hermes ships eight `lcm_*` agent tools, a `/lcm` slash command with ~25 subcommands, two lifecycle hooks (`pre_llm_call`, `post_llm_call`), and the context-engine slot itself. We must pick a distribution model before writing `pyproject.toml`.

## Options considered

### Option A: Entry-point group `hermes_agent.plugins` (pip-installed)

- Description: ship as a real Python distribution; `pip install lossless-hermes` registers an entry point that Hermes loads at startup.
- Pros:
  - Full `PluginContext` API available — `register_context_engine`, `register_hook`, `register_command`, `register_cli_command`, etc. all work (`hermes_cli/plugins.py:287-665`).
  - Standard PyPI / `uv pip install` workflow; no manual file-copy step.
  - Versioning, dependencies, and uninstall handled by pip — operators get a coherent install/upgrade story.
  - Plays well with `uv lock` and reproducible installs.
- Cons:
  - Operator must install a separate package alongside Hermes; can't "just drop files" into `~/.hermes/plugins/`.
  - Discovery requires `pip install` to have run in the same Python env Hermes uses.
- Evidence cited:
  - Entry-point group name: `hermes_cli/plugins.py:170` (`ENTRY_POINTS_GROUP = "hermes_agent.plugins"`).
  - Entry-point load loop: `hermes_cli/plugins.py:1039-1063`.
  - `dependencies.md` lines 96-102 documents the recommended `[project.entry-points."hermes_agent.plugins"]` block.
  - `hermes-hooks.md` lines 241-284 shows the full `register(ctx)` shape needed by lossless-hermes (engine + two hooks + one slash command).

### Option B: Directory mode under `~/.hermes/plugins/lossless-hermes/`

- Description: ship as a directory tarball; operator extracts to `~/.hermes/plugins/lossless-hermes/` with a `plugin.yaml` and `__init__.py`.
- Pros:
  - No pip install needed; pure copy-paste.
  - In-tree development is easy — point Hermes at a working tree.
- Cons:
  - **Context-engine directory plugins cannot register hooks.** `_EngineCollector.register_hook` is an explicit no-op (`plugins/context_engine/__init__.py:212-213`, confirmed in spike 002 step 7). This kills `pre_llm_call` and `post_llm_call` registration — both load-bearing for LCM's `ingest()` and `assemble()`.
  - `_EngineCollector` exposes a deliberately narrow surface: only `register_context_engine` is functional. No `register_command`, no `register_cli_command`, no `register_tool`. The 25 `/lcm` subcommands would be unreachable.
  - Dependency management is on the operator (no `pip install` chain to pull `httpx`, `sqlite-vec`, `pydantic`, `pyyaml`, `tenacity`).
  - Two install paths for users to learn (pip OR directory) doubles documentation and support surface.
- Evidence cited:
  - `_EngineCollector.register_hook` no-op: `plugins/context_engine/__init__.py:212-213` (per spike 002 §Method step 7).
  - Spike 002 §"The key question" — directory-mode strips hooks; `register_context_engine` is the only path that works.
  - `hermes-hooks.md` `PluginContext` table lines 127-138 — full surface only available in entry-point mode.

### Option C: Hybrid (entry-point primary, directory fallback)

- Description: ship as entry-point primarily; also publish a directory tarball for air-gapped installs.
- Pros: same as Option A plus an offline path.
- Cons: doubles release artifacts and documentation; the directory variant still loses hooks/commands (per Option B cons). Operators would silently get a degraded plugin in directory mode.
- Evidence: same as Option B — there is no way to recover hooks under directory mode without a Hermes upstream change.

## Decision

Chosen: **Option A — entry-point via `[project.entry-points."hermes_agent.plugins"]`**.

`pyproject.toml`:

```toml
[project.entry-points."hermes_agent.plugins"]
lossless-hermes = "lossless_hermes:register"
```

## Rationale

Directory mode (Option B) is structurally insufficient: `_EngineCollector` is the only loader that runs for context-engine directory plugins, and its `register_hook` is a no-op (`/Volumes/LEXAR/Claude/hermes-agent/plugins/context_engine/__init__.py:212-213`). Without `pre_llm_call` we cannot do always-on assembly; without `post_llm_call` we cannot ingest. Spike 002 confirmed this empirically.

Entry-point mode (Option A) is the only channel where `PluginContext` exposes the full surface we need:

- `register_context_engine(LCMEngine())` — the engine slot.
- `register_hook("pre_llm_call", ...)` — assembly path.
- `register_hook("post_llm_call", ...)` — ingest path.
- `register_command("lcm", lcm_dispatcher, ...)` — the 25-subcommand `/lcm` UX.

This matches the recommended shape documented at `docs/reference/dependencies.md:96-102` and the worked example in `docs/reference/hermes-hooks.md:241-284`.

The hybrid (Option C) was rejected because the directory variant is structurally degraded; shipping a knowingly-broken second artifact creates more support load than it removes.

## Consequences

- **Install workflow:** `uv pip install lossless-hermes` (or `pip install lossless-hermes`) into the same Python environment Hermes is installed in. This is documented in the README's quickstart.
- **`plugin.yaml` is not authoritative.** Pip-installed plugins go through the entry-point path and do NOT need `plugin.yaml` (`hermes_cli/plugins.py:1039-1063`). We may still ship one as documentation, but the entry point is the source of truth.
- **Operator must add `lossless-hermes` to `plugins.enabled` in `config.yaml`.** Hermes uses opt-in plugin discovery (`hermes-hooks.md` §"Plugin discovery order assumes plugins.enabled is set"). Installation docs MUST instruct adding it to the allowlist.
- **`config.yaml` must also set `context.engine: lcm`** to select us as the active engine (`run_agent.py:2256-2287` selection ladder; `hermes-hooks.md` §"context.engine selection").
- **The plugin's `register()` runs once at Hermes startup.** Heavy init (DB open, migration ladder run) belongs in `ContextEngine.on_session_start`, not in `register()` — see `hermes-hooks.md` line 326.
- **Precluded:** dropping plugin files into `~/.hermes/plugins/` is no longer a supported install path for v0.1. We document "use pip" loudly.
- **Invariant:** the package's top-level `lossless_hermes:register` callable must remain stable across versions — it is the entry-point binding.

## Open questions / 5% uncertainty

1. **Hermes upstream renames the entry-point group.** `hermes_agent.plugins` is hard-coded at `hermes_cli/plugins.py:170`. If Hermes ever renames this (e.g. to `hermes.plugins` or `hermes_agent.v2.plugins`), our entry-point binding is stale. Mitigation: integration test pins a specific Hermes version; subscribe to release notes; ship a compat shim on bump.
2. **Air-gapped install operators.** Some operators install Hermes via `curl|bash` on a host with no PyPI access. They will need a `pip install --no-index --find-links` workflow with a pre-fetched wheel. Document but don't engineer a separate path for this.
3. **Plugin discovery silently skips on import error.** If `lossless_hermes:register` raises at import (e.g. missing `sqlite-vec`), Hermes logs and continues without the plugin (`hermes_cli/plugins.py:1218-1232`). The user sees no LCM tools and may not notice. Mitigation: ship a startup health-check that emits an actionable error before any user-facing failure (see `dependencies.md` Risk row "Hermes upstream removes entry-point group").