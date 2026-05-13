---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-00] scaffolding: hermes_bridge.py — re-export ContextEngine + plugin SDK seam'
labels: 'port, scaffolding, plugin'
---

## Source (TypeScript)
- File: `lossless-claw/src/openclaw-bridge.ts`
- Lines: 26 LOC (entire file)
- Function(s)/class(es): re-exports OpenClaw plugin-SDK symbols (`@openclaw/plugin-sdk` types — `ContextEngine`, `PluginContext`, hook-registration shapes)

Per `docs/reference/lcm-source-map.md` line 51 and line 216 (the DROP list), `openclaw-bridge.ts` is dropped wholesale and replaced by `src/lossless_hermes/hermes_bridge.py` — ~30 LOC of Hermes-side re-exports. Per ADR-024 §"Hermes bridge" (lines 171-173):

> `src/lossless_hermes/hermes_bridge.py` (~30 LOC) replaces `src/openclaw-bridge.ts` (26 LOC, lcm-source-map §"Entry & top-level"). It is the seam between LCM internals and the Hermes plugin SDK: re-exports `PluginContext`, `ContextEngine`, hook-registration shapes, and any Hermes-side types LCM modules import. Centralizing the seam in one file means future Hermes ABC churn touches one file, not 50.

## Target (Python)
- File: `src/lossless_hermes/hermes_bridge.py`
- Estimated LOC: ~30 LOC (matches `openclaw-bridge.ts`)

## Dependencies
- Depends on: #00-01 (needs `src/lossless_hermes/` package skeleton)
- Blocks: #00-06 (no-op engine imports `ContextEngine` from this file, not directly from `agent.context_engine`)

## Acceptance criteria
- [ ] `src/lossless_hermes/hermes_bridge.py` exists.
- [ ] Re-exports at module level:
  - [ ] `ContextEngine` (from `agent.context_engine` — the Hermes ABC; cited by the Hermes plugin-glue porting guide and `hermes-hooks.md` line 326 worked example)
  - [ ] `PluginContext` (from `hermes_agent.plugins` or the canonical Hermes module path — verify against `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:287-665`)
  - [ ] `load_config` and `cfg_get` (from `hermes_cli.config` per `hermes-hooks.md` line 259)
  - [ ] `get_hermes_home` (from `hermes_constants` per `hermes-hooks.md` line 260)
- [ ] Imports are guarded so an ImportError surfaces a structured, actionable message (per ADR-007 §Consequences "Startup health-check required"): if `agent.context_engine` cannot be imported, the module raises a `LosslessHermesEnvironmentError` (or similar) with text like "lossless-hermes was installed in an environment without hermes-agent on the path. Install Hermes first — see https://github.com/NousResearch/hermes-agent#install."
- [ ] Module docstring cites: ADR-024 §"Hermes bridge", ADR-001 §Consequences, and the source `openclaw-bridge.ts` (with note "DROPPED — see lcm-source-map.md line 216").
- [ ] No business logic. The file is import + re-export only. No transformations, no wrappers.
- [ ] Every Hermes-side symbol that any future `src/lossless_hermes/**/*.py` file needs MUST be re-exported here. Direct `from agent.context_engine import ...` calls outside this file are forbidden by an `__all__` discipline + ruff rule (added to ruff config in a follow-up; for v0.1, enforced by code review).
- [ ] A trivial smoke test `tests/test_hermes_bridge.py` asserts the bridge can be imported and that `ContextEngine` is exported (skipping the test if Hermes is not installed in CI — Hermes install in CI is tracked in ADR-007 §Consequences "CI must install Hermes for integration tests"; v0.1 may use a `pytest.importorskip` guard until then).
- [ ] Type hints on every re-exported name (so `ty check` sees them as proper bindings, not `Any`).

## Estimated effort
3 hours

## Confidence
90% — the bridge shape is well-specified, but exact Hermes module paths (`agent.context_engine` vs `hermes_agent.context_engine` vs something else) must be verified against the live Hermes source before merge. Mitigation: read `/Volumes/LEXAR/Claude/hermes-agent/agent/context_engine.py` and `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/config.py` before starting.

## Files to read before starting
- `docs/adr/024-project-layout.md` §"Hermes bridge" lines 171-173 (decision + rationale)
- `docs/adr/001-plugin-distribution-model.md` (entry-point group `hermes_agent.plugins`)
- `docs/adr/007-hermes-as-dependency.md` §Consequences (startup health-check requirement)
- `docs/reference/hermes-hooks.md` lines 241-284 (worked example showing every Hermes-side import the plugin makes)
- `docs/reference/lcm-source-map.md` line 216 (DROP list entry for `openclaw-bridge.ts`)
- Live source: `/Volumes/LEXAR/Claude/hermes-agent/agent/context_engine.py` (the `ContextEngine` ABC)
- Live source: `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:170,287-665,1039-1063` (`PluginContext` surface + entry-point group name)
- Live source: `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/config.py` (`load_config`, `cfg_get`)
- Live source: `/Volumes/LEXAR/Claude/hermes-agent/hermes_constants.py` (`get_hermes_home`)
