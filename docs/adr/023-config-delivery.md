# ADR-023: Configuration delivery

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

Operators need a single, documented place to configure the lossless-hermes plugin: API keys, worker intervals, Voyage model choices, compaction thresholds, debug-logging flags, and feature toggles. The TS source mostly configures via env vars (`LCM_SUMMARY_MODEL`, `LCM_TOOL_RESULT_TOKEN_BUDGET`, `VOYAGE_API_KEY`) and an OpenClaw plugin-config object (`config.plugins.entries["lossless-claw"].config.*`). The Hermes host already has a canonical YAML config at `~/.hermes/config.yaml`, and existing Hermes plugins consume their config via namespaced top-level keys (e.g., `memory.*`, `image_gen.*`).

The question: where does the operator put plugin config, and what naming convention does the namespace use?

Constraints:
- Hermes config delivery is `~/.hermes/config.yaml`. No alternative.
- Existing Hermes precedent: each plugin owns a top-level key (snake_case Python convention).
- The plugin's PyPI name and CLI name are `lossless-hermes` (hyphenated). The Python module is `lossless_hermes` (underscored, Python identifier rules).
- Pydantic v2 is already in Hermes's dep tree for typed config validation; no new dep.

## Options considered

### Option A: `context.lossless_hermes.*` namespace in `~/.hermes/config.yaml`, snake_case keys, pydantic v2 schema

- Description: top-level YAML key `lossless_hermes:` with snake_case fields. A Pydantic v2 model (`LosslessHermesConfig`) loads from the YAML subtree, validates types, applies defaults, surfaces errors with field paths. Inline `${VAR}` interpolation works via Hermes's standard config-loader.

Example:

```yaml
lossless_hermes:
  voyage_api_key: "${VOYAGE_API_KEY}"
  voyage_embed_model: "voyage-4-large"
  voyage_rerank_model: "rerank-2.5"
  summary_model: "claude-sonnet-4"
  context_threshold: 0.75
  leaf_chunk_tokens: 20000
  workers:
    embedding_backfill:
      interval_s: 60
    entity_extraction:
      interval_s: 30
    condensation_maintenance:
      interval_s: 120
  agent_compaction_tool_enabled: true
  debug_logging: false
```

- Pros:
  - Matches Hermes plugin-config precedent (`memory.*`, `image_gen.*`).
  - snake_case matches Python identifier rules and is conventional for Hermes plugin config.
  - Pydantic v2 already in the host's deps — zero new dep. Validation errors include field paths and types.
  - `${VAR}` interpolation supports the secret-management workflow operators already use elsewhere.
  - Single source of truth — one YAML file, one schema, one place for operators to look.
  - Maps cleanly to the LCM TS env-var knobs: each `LCM_*` env var has a snake_case counterpart in the config schema. Env vars still work for CI overrides via `os.environ` fallback inside the loader.
- Cons:
  - Namespace name `lossless_hermes` differs from the hyphenated package name `lossless-hermes`. Cosmetic mismatch. Mitigated: documented up-front; matches the Python module naming.
- Evidence cited:
  - `plugin-glue.md`: Hermes plugin-config precedent — `memory.*` and `image_gen.*` plugin-config namespacing.
  - `lossless-hermes` PyPI name vs `lossless_hermes` module name is standard Python packaging (PEP 8 distribution-vs-module).

### Option B: Env-var-only configuration

- Description: every knob is a `LCM_*` or `LOSSLESS_HERMES_*` env var.
- Pros: CI-friendly. No YAML parsing.
- Cons:
  - Doesn't match Hermes precedent. Operators have a single config.yaml; sprinkling env vars across system services is friction.
  - Limited type richness — nested structures (per-worker intervals) become ugly underscored names (`LCM_WORKERS_EMBEDDING_BACKFILL_INTERVAL_S=60`).
  - No `${VAR}` interpolation pattern; everything is literal.

### Option C: TOML config in a separate file

- Description: `~/.hermes/lossless-hermes.toml` with all knobs.
- Pros: per-plugin config file is tidy.
- Cons:
  - Doesn't match host precedent. Other Hermes plugins all live in the central YAML; one plugin doing TOML is surprising.
  - One more file for operators to find and back up.

### Option D: Hyphenated namespace name (`lossless-hermes` in YAML)

- Description: YAML top-level key is `lossless-hermes:`, matching the package name verbatim.
- Pros: name matches the package.
- Cons:
  - Hyphens in YAML keys need quoting or break Python attribute-style access (`config["lossless-hermes"]` vs `config.lossless_hermes`).
  - Inconsistent with snake_case Hermes precedent.
  - Pydantic field names would need an alias to bridge.

## Decision

Chosen: **Option A (`lossless_hermes.*` namespace in `~/.hermes/config.yaml`, snake_case keys, pydantic v2 validation)**.

## Rationale

- Matches Hermes plugin-config precedent. Operators who know `memory.*` and `image_gen.*` find `lossless_hermes.*` immediately.
- snake_case is the Python convention and matches the module name (`lossless_hermes`), the directory layout (`src/lossless_hermes/`), and Pydantic field-name idioms. The hyphenated `lossless-hermes` is the PyPI distribution name only — operators never type it in config.
- Pydantic v2 gives typed validation, default values, field-path error messages, and `Field(env="VOYAGE_API_KEY")` for env-var fallback. Already a dep in Hermes — zero new surface.
- Single YAML file for the operator. Backups, version control, code review all work as they would for any Hermes config change.
- Env vars survive as a CI override mechanism: any field can be sourced from `os.environ` via the Pydantic loader, and explicit `${VAR}` interpolation in the YAML body handles secret-management workflows.

## Consequences

- New module: `src/lossless_hermes/config.py`. Defines:
  - `class LosslessHermesConfig(BaseModel)` — Pydantic v2 model with all knobs.
  - `class WorkerConfig(BaseModel)` — nested model for `workers.*`.
  - `load_config(path: Path | None = None) -> LosslessHermesConfig` — reads `~/.hermes/config.yaml` (or override path), extracts the `lossless_hermes:` subtree, performs `${VAR}` interpolation via Hermes's standard config-loader, instantiates the model.
- Field-name policy: every Python-identifier-safe snake_case. Nested dicts allowed via nested Pydantic models. No hyphens, no camelCase.
- Engine constructor accepts a `LosslessHermesConfig` instance. The `register(ctx)` plugin entry point calls `load_config()` once and passes the result.
- Default values match TS defaults from the LCM source (e.g., `context_threshold: 0.75`, `leaf_chunk_tokens: 20_000`, `fresh_tail_count: 8`). When the TS source uses an env-var fallback (`LCM_SUMMARY_MODEL`), the Pydantic field accepts the same env name as a fallback source (`Field(default=None, validation_alias=AliasChoices("summary_model", "LCM_SUMMARY_MODEL"))` or similar).
- The plugin name is `lossless-hermes` in user-facing artifacts (PyPI, `pip install`, `pyproject.toml [project.name]`, CLI `lcm-...` commands). The config namespace is `lossless_hermes` (underscored). This naming-convention split is documented prominently in the README.
- Operator changes to config require a plugin reload (handled by Hermes session lifecycle). Hot-reload of config is not in scope.
- ADR-022 (Voyage credential resolution) layers on top: the `voyage_api_key` field in this config is tier-1 of the three-tier resolver.
- Tests:
  - Loading a minimal YAML (just `lossless_hermes: {}`) yields the model with all defaults.
  - `${VOYAGE_API_KEY}` interpolation works.
  - An unknown field raises a typed `pydantic.ValidationError` (catch typos at startup, not at first use).
  - Env-var aliases (where defined) take precedence over YAML when set.

## Open questions / 5% uncertainty

- **Schema versioning.** If a future release renames a field, operators on the old YAML break. Mitigation: `pydantic` `validation_alias` to accept old + new names during a transition window; deprecate-then-remove cycle.
- **Hot reload.** Not supported in v1. If an operator changes the config, they restart the Hermes session. Documented as a limitation.
- **CLI override on top.** A future `lcm` CLI might want `--config-override` flags. Not in scope; if added, they layer above the YAML (highest precedence).
- **Naming-convention split visibility.** `lossless-hermes` (PyPI) vs `lossless_hermes` (config + module) might confuse first-time operators. Mitigated by an explicit README section and a startup-banner log line that names both.
- **Backward compatibility with OpenClaw `lossless-claw` config block.** Some OpenClaw users may have an existing `lossless_claw:` or similar block. We do NOT auto-migrate; document the new namespace in upgrade guides. A Phase-2 `lcm config import-openclaw` command can lift OpenClaw plugin-config into Hermes if needed.
