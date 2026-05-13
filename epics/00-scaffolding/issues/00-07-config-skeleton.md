---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-00] scaffolding: pydantic v2 LcmConfig skeleton in db/config.py'
labels: 'port, scaffolding, config'
---

## Source (TypeScript)
- File: `lossless-claw/src/plugin/index.ts` (OpenClaw config object reading), `lossless-claw/src/db/config.ts` (DB-side config bag — pragmas, paths, feature flags)
- Lines: ~100-200 LOC of config plumbing (the OpenClaw-side config reader); LCM TS doesn't have a Pydantic equivalent — config types are TypeScript interfaces hand-shaped from the `config.plugins.entries["lossless-claw"].config.*` blob
- Function(s)/class(es): `loadLcmConfig` (TS, in OpenClaw plugin glue), various env-var fallbacks (`LCM_SUMMARY_MODEL`, `LCM_TOOL_RESULT_TOKEN_BUDGET`, etc.)

## Target (Python)
- File: `src/lossless_hermes/db/config.py`
- Estimated LOC: ~80-120 LOC (skeleton with empty model + loader)

> **Path note:** Per the task spec, this issue puts the skeleton at `src/lossless_hermes/db/config.py`. ADR-023 §Consequences names the file `src/lossless_hermes/config.py` (without the `db/` prefix). ADR-024 §Decision shows BOTH a `db/config.py` (DB-side config — pragmas, driver selection) AND would expect a top-level `config.py` for operator config. Recommendation for the implementer: ship the operator config in `db/config.py` for v0 and revisit if a second config concern emerges. The path can be renamed in a follow-up without breaking the entry-point binding (which only references `lossless_hermes:register`).

## Dependencies
- Depends on: #00-01 (`pydantic==2.12.5` + `pyyaml==6.0.3` installed via the runtime deps)
- Blocks: #00-06 (the no-op engine's `__init__` takes a `LcmConfig` instance)

## Acceptance criteria
- [ ] `src/lossless_hermes/db/config.py` exists.
- [ ] Defines `class LcmConfig(pydantic.BaseModel)` — Pydantic v2 model.
- [ ] Model is **empty for v0** (no fields). It must be instantiable as `LcmConfig()` with no args; every real knob lands in later issues as the relevant subsystem ports.
- [ ] `model_config = ConfigDict(extra='forbid')` — unknown fields raise `pydantic.ValidationError` at startup (per ADR-023 §Consequences "An unknown field raises a typed `pydantic.ValidationError` (catch typos at startup, not at first use)").
- [ ] Defines `def load_config(path: Path | None = None) -> LcmConfig`:
  - [ ] Defaults `path` to `get_hermes_home() / "config.yaml"` (the canonical Hermes config location).
  - [ ] If the file does not exist, returns `LcmConfig()` (default-everything).
  - [ ] If the file exists, parses it with `yaml.safe_load`.
  - [ ] Extracts the `lossless_hermes:` subtree per ADR-023 §Decision. (The task spec mentions `context.lossless_hermes.*`; resolve in this issue by reading ADR-023 carefully — the canonical namespace per ADR-023 Option A is **`lossless_hermes:` at YAML top level**, not nested under `context:`. The `context.engine: lcm` selector lives at top level; the plugin's own config keys live at `lossless_hermes:`. Document this clearly in the loader's docstring.)
  - [ ] Performs `${VAR}` env-var interpolation via Hermes's config-loader helper (`cfg_get` from `hermes_cli.config` — already re-exported by `hermes_bridge.py` per #00-05).
  - [ ] Returns `LcmConfig(**subtree)`.
- [ ] Defines `class WorkerConfig(pydantic.BaseModel)` — empty nested model. Placeholder for the workers map that lands in Epic 02 (worker loop dispatcher per ADR-020).
- [ ] Module docstring cites ADR-023 §Decision and §Consequences, and notes that field additions are deliberate (each new field is a separate PR that ports the corresponding TS knob).
- [ ] No env-var fallback wiring in v0 — Pydantic's `Field(default=..., validation_alias=AliasChoices(...))` shape is documented in a comment but not used until a real knob lands (per ADR-023 §Consequences "When the TS source uses an env-var fallback (`LCM_SUMMARY_MODEL`), the Pydantic field accepts the same env name as a fallback source").
- [ ] Smoke test `tests/test_config_load.py`:
  - [ ] Empty YAML (`lossless_hermes: {}`) → `LcmConfig()` with defaults.
  - [ ] Missing file → `LcmConfig()` with defaults.
  - [ ] YAML with an unknown key under `lossless_hermes:` → raises `pydantic.ValidationError`.
  - [ ] `${HERMES_TEST_VAR}` in YAML body is interpolated when the env var is set (using `monkeypatch.setenv`).
- [ ] `LcmConfig` is imported and used by #00-06's `LCMEngine.__init__`.
- [ ] No PII or secrets in any default value (config-skeleton must not bake in test API keys).

## Estimated effort
4 hours

## Confidence
95% — ADR-023 §Consequences fully specifies the loader shape. The 5% residual is the YAML namespace resolution (top-level `lossless_hermes:` per ADR-023, vs the task spec's `context.lossless_hermes.*`). Recommendation: implement per ADR-023 (top-level) and update the README quickstart in #00-08 to match. If the implementer finds a documented reason to nest under `context.`, raise the question on the issue thread before deviating.

## Files to read before starting
- `docs/adr/023-config-delivery.md` (entire ADR — §Consequences has the loader spec)
- `docs/adr/024-project-layout.md` §Decision (where `db/config.py` lives + the nearby `db/connection.py`/`db/features.py`/`db/migration.py` files this neighbors)
- `docs/reference/dependencies.md` lines 15-16 (pydantic + pyyaml pins)
- `docs/reference/hermes-hooks.md` lines 306-318 (`config.yaml` worked example — `context.engine: lcm` is at top level; `plugins.enabled: [lossless-hermes]` is at top level; the plugin's own config keys live under `lossless_hermes:`)
- Live source: `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/config.py` (`load_config`, `cfg_get` — what `${VAR}` interpolation looks like in Hermes today)
