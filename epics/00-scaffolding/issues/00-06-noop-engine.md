---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-00] scaffolding: no-op LCMEngine + register(ctx) wiring'
labels: 'port, scaffolding, plugin, engine'
---

## Source (TypeScript)
- File: `lossless-claw/src/engine.ts` (the `LcmContextEngine` class shell — class declaration + lifecycle methods + the OpenClaw plugin entry point under `src/plugin/index.ts`)
- Lines: ~8,731 LOC total in `engine.ts` (split across responsibilities per ADR-027); ~2,804 LOC in `plugin/index.ts`. **This issue ports only the class skeleton + no-op `compress()` — every other method becomes `raise NotImplementedError` and is filled in by later epics.**
- Function(s)/class(es): `LcmContextEngine` (class shell only), `register()` (Hermes-side, replaces OpenClaw `registerPlugin`)

## Target (Python)
- File: `src/lossless_hermes/engine/__init__.py`, `src/lossless_hermes/__init__.py` (fill in the `register()` stub from #00-01)
- Estimated LOC: ~80-120 LOC (engine class shell) + ~30-50 LOC (register())

## Dependencies
- Depends on:
  - #00-01 (package skeleton + entry point declared)
  - #00-05 (hermes_bridge.py for `ContextEngine` and `PluginContext` re-exports)
  - #00-07 (config skeleton — `LcmConfig` is passed to the engine constructor)
- Blocks: every later issue that needs the engine to exist (Epic 02 onward)

## Acceptance criteria
- [ ] `src/lossless_hermes/engine/__init__.py` exists and declares `class LCMEngine(ContextEngine)` where `ContextEngine` is imported from `lossless_hermes.hermes_bridge` (per #00-05).
- [ ] `LCMEngine.name = "lcm"` — string class attribute. This is the value that matches `context.engine: lcm` in `~/.hermes/config.yaml` (per ADR-001 §Consequences).
- [ ] `LCMEngine.__init__(self, hermes_home: Path, config: LcmConfig)` accepts the two args the worked example in `hermes-hooks.md` lines 264-269 passes. Stores them as instance attributes; no heavy init (per ADR-001 §Consequences "Heavy init (DB open, migration ladder run) belongs in `ContextEngine.on_session_start`, not in `register()`").
- [ ] `LCMEngine.compress(self, messages, current_tokens=None, focus_topic=None)` is a **no-op passthrough** that returns `messages` verbatim. Round-trip property test in `tests/test_engine_noop.py` asserts `engine.compress(msgs) == msgs` for a variety of message shapes (empty list, single message, multi-turn, multi-modal content blocks).
- [ ] `LCMEngine.should_compress(...)` returns `False` unconditionally (no compaction in v0).
- [ ] Lifecycle stubs are present but raise `NotImplementedError` with messages naming the epic that fills them in:
  - [ ] `on_session_start(...)` — `"on_session_start lands in Epic 02 (engine skeleton)"`
  - [ ] `on_session_end(...)` — same
  - [ ] `on_session_reset(...)` — same
  - [ ] `get_tool_schemas(...)` — returns `[]` for v0 (no tools yet — tools land in Epic 06)
  - [ ] `handle_tool_call(...)` — raises `NotImplementedError("tools land in Epic 06")`
- [ ] `src/lossless_hermes/__init__.py` fills in the `register(ctx)` callable per `hermes-hooks.md` lines 256-284, scoped to v0:
  - [ ] Loads `LcmConfig` via `lossless_hermes.db.config.load_config()` (from #00-07)
  - [ ] Constructs `LCMEngine(hermes_home=get_hermes_home(), config=config)`
  - [ ] Calls `ctx.register_context_engine(engine)`
  - [ ] Does **NOT** register `pre_llm_call` / `post_llm_call` hooks in v0 (the no-op engine has no `ingest`/`assemble` — those land in Epic 03). Lines for the hook registration are present but commented out with a `# TODO(epic-03): wire hooks` marker.
  - [ ] Does **NOT** register the `/lcm` command in v0 (it has no subcommands yet — Epic 08).
- [ ] Startup health-check (per ADR-007 §Consequences): `register()` wraps the entire body in a `try/except ImportError` that emits a structured error if `agent.context_engine` is missing — "lossless-hermes is installed in an environment without hermes-agent on the path. Install Hermes first."
- [ ] Apple `/usr/bin/python3` guard (per ADR-004 §Consequences): at module import time, `LCMEngine.__init__` checks that `sqlite3.Connection` has `enable_load_extension` and raises a clear error if not — "Apple system Python is unsupported; install Homebrew Python, pyenv, or uv-managed Python." (This guard fires before any DB open attempt.)
- [ ] Smoke test `tests/test_register.py` uses a stub `PluginContext` (a `Mock` with `register_context_engine`/`register_hook`/`register_command` attributes) and asserts `register(ctx)` calls `ctx.register_context_engine` exactly once with an `LCMEngine` instance.
- [ ] Round-trip test: instantiate `LCMEngine(hermes_home=tmp_path, config=LcmConfig())`, call `engine.compress(["hello", "world"])`, assert result == `["hello", "world"]`.
- [ ] `LCMEngine.name == "lcm"` (string equality, not the class name).

## Estimated effort
4 hours

## Confidence
90% — the engine class shell is well-specified by ADR-001 + `hermes-hooks.md` worked example. The 10% residual is the exact signature of `ContextEngine.compress()` and `should_compress()` — verify against `/Volumes/LEXAR/Claude/hermes-agent/agent/context_engine.py` line-by-line before starting. ABC signature drift is the only realistic blocker.

## Files to read before starting
- `docs/adr/001-plugin-distribution-model.md` (entire ADR — §Consequences has the heavy-init prohibition + `context.engine: lcm` selection)
- `docs/adr/007-hermes-as-dependency.md` §Consequences (startup health-check)
- `docs/adr/004-sqlite3-backend.md` §Consequences (Apple system Python guard)
- `docs/adr/024-project-layout.md` (where the engine package lives + ADR-027 split — for v0, only `engine/__init__.py` exists)
- `docs/adr/027-engine-splitting.md` (skeleton-only port for v0; ingest/assemble/compact land in Epic 02-04)
- `docs/reference/hermes-hooks.md` lines 256-326 (full worked example of `register()` + `ContextEngine` hook landing table)
- Live source: `/Volumes/LEXAR/Claude/hermes-agent/agent/context_engine.py` (the ABC — copy method signatures verbatim)
