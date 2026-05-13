# lossless-hermes

[![CI](https://github.com/electricsheephq/lossless-hermes/actions/workflows/ci.yml/badge.svg)](https://github.com/electricsheephq/lossless-hermes/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

Lossless Context Management plugin for **[Hermes-agent](https://github.com/NousResearch/hermes-agent)**, ported from [Martian-Engineering/lossless-claw](https://github.com/Martian-Engineering/lossless-claw) (TypeScript/OpenClaw) to Python/Hermes. **v0.1 is a green-CI scaffolding release** — the plugin registers and loads, but the context engine is a no-op passthrough. Real LCM behavior (storage, ingest/assembly, compaction, embeddings, tools, entities, ops, eval) lands incrementally across Epics 01–09 — see [`ROADMAP.md`](./ROADMAP.md).

## Status: 🟡 Wave 1 — Epic 00 Scaffolding (in progress)

Phase 1 (architecture & planning) is complete. Architecture, decisions, risks, and the full epic/issue breakdown live under [`docs/`](./docs/) and [`epics/`](./epics/). Phase 2 (execution) is underway — current wave and last merged PR are tracked in [`STATUS.md`](./STATUS.md).

## Install

Lossless-hermes is a **Hermes plugin**. Install Hermes first, then install the plugin into the same Python environment.

### 1. Install Hermes

Per [Hermes's README](https://github.com/NousResearch/hermes-agent#quick-install), the recommended path on Linux, macOS, WSL2, or Termux is the curl one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

Hermes is not on PyPI (see [ADR-007](./docs/adr/007-hermes-as-dependency.md)), so the plugin does **not** pin `hermes-agent` as a dependency — operators install it separately.

Alternative (source install for contributors):

```bash
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent
uv pip install -e ".[all,dev]"
```

### 2. Install the plugin

Once Hermes is on your `PATH` (and `python -c "import agent.context_engine"` succeeds), install lossless-hermes into the **same Python environment**:

```bash
# Recommended (uv-managed env)
uv pip install lossless-hermes

# Or plain pip
pip install lossless-hermes
```

For development against a checkout:

```bash
git clone https://github.com/electricsheephq/lossless-hermes
cd lossless-hermes
uv pip install -e ".[dev]"
```

The `[dev]` extra pulls in `pytest`, `ruff`, `ty`, `pre-commit`, and `respx` — see [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the dev loop.

Discovery happens via the `hermes_agent.plugins` entry-point group declared in [`pyproject.toml`](./pyproject.toml) (per [ADR-001](./docs/adr/001-plugin-distribution-model.md)) — no manual file copy into `~/.hermes/plugins/` is required or supported.

## Quickstart

After installing Hermes and lossless-hermes into the same environment, edit `~/.hermes/config.yaml` to enable the plugin and select LCM as the active context engine. Both settings are required per [ADR-001 §Consequences](./docs/adr/001-plugin-distribution-model.md#consequences) — adding to `plugins.enabled` alone is not enough; LCM also has to win the `context.engine` selection ladder.

```yaml
context:
  engine: lcm

plugins:
  enabled:
    - lossless-hermes
```

Verify the plugin registers:

```bash
hermes
```

A startup log line confirms `lossless-hermes` is loaded and the context engine is `lcm`. The v0.1 engine is a no-op passthrough (it returns the conversation unchanged), so a session works exactly as it would without the plugin — that is the deliberate scaffolding-release behavior.

For plugin-specific configuration (Voyage keys, model choices, worker intervals, compaction thresholds), the namespace is `lossless_hermes:` (snake_case — see [Naming convention](#naming-convention) below). The full schema lands with Epic 02 (engine skeleton) and is wired up in [issue 00-07](./epics/00-scaffolding/issues/00-07-config-skeleton.md).

## Platform support

Per [`docs/reference/dependencies.md`](./docs/reference/dependencies.md) §"Platform support matrix" and the spike results in [`docs/spike-results/`](./docs/spike-results/). Cells marked **tested** were exercised first-hand during spikes 001 and 005; **inferred** is strong inference from PyPI wheel availability + CPython build defaults but not yet locally run (CI matrix will convert these — see [ADR-005 §Consequences](./docs/adr/005-python-version.md#consequences)).

| Platform | Python source | Supported | Notes |
|---|---|---|---|
| macOS arm64 | Homebrew `python@3.12` | YES (tested) | **Recommended default.** |
| macOS arm64 | Homebrew `python@3.11`, `python@3.13`, `python@3.14` | YES (3.13 tested; others inferred) | Same code path. |
| macOS arm64 | `/usr/bin/python3` (Apple system, 3.9.6) | **NO** | Below the `>=3.11` floor AND missing `enable_load_extension` (spike 001). See warning below. |
| macOS arm64 | python.org installer | YES (inferred) | CI matrix close-out planned. |
| macOS x86_64 | Homebrew | YES (inferred) | Wheel coverage confirmed. |
| Linux x86_64 / arm64 | system / deadsnakes / uv | YES (inferred) | Hermes runs FTS5 + trigram on stdlib `sqlite3` in production on Linux. |
| Linux musl / Alpine | `python:3.13-alpine` | YES (inferred) | Add to CI if needed. |
| Windows WSL2 | Ubuntu Python | YES (inferred) | **Recommended Windows path.** |
| Windows (native) | python.org installer | **Out of scope for v0.1** | Hermes itself tags native Windows as "early beta"; use WSL2. |

**Recommended Python: 3.12** (Homebrew, pyenv, or uv-managed) per [ADR-005 §Decision](./docs/adr/005-python-version.md#decision) — best performance, mature toolchain, broadest spike coverage.

> [!WARNING]
> **Apple `/usr/bin/python3` is UNSUPPORTED.** It is Python 3.9.6 (below the `>=3.11` floor) and its `sqlite3` module is built **without** `enable_load_extension`, which prevents `sqlite-vec` from loading at all. See [ADR-004 §Consequences](./docs/adr/004-sqlite3-backend.md#consequences) and [ADR-005 §Consequences](./docs/adr/005-python-version.md#consequences). One-line fix: `brew install python@3.12` (or use `uv python install 3.12`), then re-run the install steps against that interpreter.

## Naming convention

Three names refer to "this plugin." They are deliberately distinct (per [ADR-023 §Open questions](./docs/adr/023-config-delivery.md#open-questions--5-uncertainty) — naming-convention-split visibility):

| Form | Where it appears | Example |
|---|---|---|
| `lossless-hermes` (hyphenated) | PyPI distribution name, `pyproject.toml [project.name]`, `pip install` / `pip uninstall` commands | `uv pip install lossless-hermes` |
| `lossless_hermes` (snake_case, importable) | Python module name, `import lossless_hermes`, file/directory paths under `src/` | `from lossless_hermes import register` |
| `lossless_hermes:` (snake_case, YAML) | `~/.hermes/config.yaml` namespace key for plugin-specific configuration | `lossless_hermes:`<br>`  voyage_api_key: "${VOYAGE_API_KEY}"` |

Operators type the **snake_case** form in `config.yaml`; they only see the **hyphenated** form when running `pip install` or `pip uninstall`. Both forms appear in the Hermes startup banner so the mapping is visible at runtime.

## What v0.1 does NOT do

This is a scaffolding release. The engine is a no-op passthrough. The following land incrementally — there is no `/lcm` command, no recall, no embeddings, no entity extraction in v0.1:

| Feature | Lands in |
|---|---|
| 27-table SQLite schema, migrations, FTS5 + sqlite-vec wiring | [Epic 01 — Storage](./epics/01-storage/) |
| `ContextEngine` round-trips messages through `compress()` | [Epic 02 — Engine skeleton](./epics/02-engine-skeleton/) |
| Per-turn ingest + always-on assembly via `pre_llm_call` / `post_llm_call` hooks | [Epic 03 — Ingest + assembly](./epics/03-ingest-assembly/) |
| Compaction (leaf summaries, condensed summaries, pyramid maintenance) | [Epic 04 — Compaction](./epics/04-compaction/) |
| Voyage embeddings + hybrid retrieval (FTS5 ∪ vec0, +52.5pp recall lift) | [Epic 05 — Embeddings](./epics/05-embeddings/) |
| 7 agent tools (`lcm_grep`, `lcm_describe`, `lcm_recall`, …) | [Epic 06 — Tools](./epics/06-tools/) |
| Entity coref pipeline + synthesis | [Epic 07 — Entity + synthesis](./epics/07-entity-synthesis/) |
| `/lcm health`, `/lcm doctor`, `/lcm import-openclaw`, ~25 subcommands | [Epic 08 — CLI + ops](./epics/08-cli-ops/) |
| Drift CI gating, recall eval suite | [Epic 09 — Eval](./epics/09-eval/) |

If you install lossless-hermes today and notice there's no `/lcm` command — that's expected. The plugin is doing what v0.1 promises: loading cleanly as a no-op so the integration surface is exercised before the engine ships.

## Source of truth

| Document | Purpose |
|---|---|
| [`ROADMAP.md`](./ROADMAP.md) | 10-epic roadmap, milestones, critical path |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | System architecture, target structure, data flow |
| [`STATUS.md`](./STATUS.md) | Current wave, last merged PR, milestone progress |
| [`BLOCKERS.md`](./BLOCKERS.md) | Open blockers and their owners |
| [`LEDGER.md`](./LEDGER.md) | Per-issue execution ledger |
| [`docs/risks.md`](./docs/risks.md) | Identified risks + mitigation status |
| [`docs/adr/`](./docs/adr/) | Architecture Decision Records (numbered, dated, status-tagged) |
| [`docs/porting-guides/`](./docs/porting-guides/) | Per-subsystem TS → Python porting guides |
| [`docs/reference/`](./docs/reference/) | Cross-reference docs (dependencies, Hermes hooks) |
| [`docs/spike-results/`](./docs/spike-results/) | De-risking spike findings |
| [`docs/upstream/`](./docs/upstream/) | Upstream Hermes patches we're tracking |
| [`epics/`](./epics/) | 10 epics, each with per-issue specifications |
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | Dev setup, test policy, lint/format, branch policy |

## Project context

- **Source**: `Martian-Engineering/lossless-claw` main + [PR #613](https://github.com/Martian-Engineering/lossless-claw/pull/613) (v4.1 omnibus, 52k LOC) + [PR #628](https://github.com/Martian-Engineering/lossless-claw/pull/628) (stub-tier, merged)
- **Target**: `NousResearch/hermes-agent` Python plugin via `ContextEngine` ABC
- **OpenClaw coupling surface**: 26 LOC in `src/openclaw-bridge.ts` (single import seam)
- **Hermes anticipation**: `agent/context_engine.py` docstring at line 5 explicitly names LCM as a planned tenant

## Quick links

- Hermes-agent repo: https://github.com/NousResearch/hermes-agent
- Source repo: https://github.com/Martian-Engineering/lossless-claw
- PR #613 (omnibus): https://github.com/Martian-Engineering/lossless-claw/pull/613
- PR #628 (stub-tier): https://github.com/Martian-Engineering/lossless-claw/pull/628
- Hermes ContextEngine ABC: https://github.com/NousResearch/hermes-agent/blob/main/agent/context_engine.py

## License

[MIT](./LICENSE) © Electric Sheep HQ.
