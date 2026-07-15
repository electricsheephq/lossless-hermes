# lossless-hermes

[![CI](https://github.com/electricsheephq/lossless-hermes/actions/workflows/ci.yml/badge.svg)](https://github.com/electricsheephq/lossless-hermes/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/electricsheephq/lossless-hermes)](https://github.com/electricsheephq/lossless-hermes/releases)
[![Python 3.11 – 3.13](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

> [!IMPORTANT]
> **This repository is archived (2026-07-15).** It was an exploratory verbatim port of LCM v4.1 and
> was never run in production. Rather than maintain a parallel plugin, we now contribute LCM features
> directly to **[stephenschoettler/hermes-lcm](https://github.com/stephenschoettler/hermes-lcm)** — the
> shipping, actively-maintained LCM plugin for Hermes — see the porting tracker
> [hermes-lcm#375](https://github.com/stephenschoettler/hermes-lcm/issues/375). If you want Lossless
> Context Management for Hermes, use `hermes-lcm`.

**Lossless Context Management for [Hermes-agent](https://github.com/NousResearch/hermes-agent).**

> The context window stays bounded. The memory doesn't. Nothing is ever dropped.

`lossless-hermes` is a context-engine plugin for Hermes. When a conversation outgrows the
model's context window, most agents truncate old turns or replace them with a flat summary —
and the original detail is gone from the prompt. `lossless-hermes` instead **persists every
message**, compacts older history into a navigable **summary DAG**, and gives the agent
**tools to drill back into the exact detail that was compacted**. A long-running conversation
keeps full recall without ever blowing the token budget.

It is a Python port of [Lossless Claw](https://github.com/Martian-Engineering/lossless-claw)
(LCM v4.1) — the design from the [LCM paper](https://papers.voltropy.com/LCM) — re-homed onto
Hermes's pluggable `ContextEngine` slot.

---

## Contents

- [The problem](#the-problem)
- [How it works](#how-it-works)
- [Why lossless-hermes](#why-lossless-hermes)
- [Install](#install)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Agent tools](#agent-tools)
- [Operator commands](#operator-commands)
- [Migrating from OpenClaw](#migrating-from-openclaw)
- [Platform support](#platform-support)
- [Status & maturity](#status--maturity)
- [Architecture](#architecture)
- [Related work](#related-work)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## The problem

Every agent eventually fills its context window. The usual remedy is a sliding window or a
flat summary: older turns are replaced by one lossy paragraph and the original material
leaves the prompt for good. When the agent later needs a specific decision, file, error, or
number from earlier in the conversation, it simply cannot see it — and recovery, where it
exists at all, lives in a separate history path the model rarely reaches for on its own.

The result is an agent that quietly gets *worse* the longer you talk to it.

## How it works

`lossless-hermes` makes recall a first-class part of the context engine:

1. **Persist** — every message is written to a local SQLite database, organized by
   conversation. Nothing is discarded, ever.
2. **Summarize** — when older history grows past a token threshold, chunks of raw messages
   are summarized into *leaf* nodes by your configured LLM.
3. **Condense** — as leaves accumulate, they are condensed into higher-level nodes, forming
   a depth-aware **summary DAG** (directed acyclic graph). Every node links back to its
   sources.
4. **Assemble** — each turn the active context is rebuilt from condensed summaries plus the
   recent raw tail, kept under the model's token budget.
5. **Recall** — the agent gets [tools](#agent-tools) to search and expand compacted history:
   any summary can be drilled back down to the original messages on demand.

```text
   ACTIVE CONTEXT  ── always under the token budget ─────────────┐
   │  ◆ condensed summaries        ▪ recent raw tail (verbatim)  │
   └──────────│──────────────────────────────│───────────────────┘
              │ expand on demand             │ never compacted
   SUMMARY DAG ▼                             │
   │  ◆ condensed ──▶ ◆ leaf ──▶ ▪ raw messages                  │
   │  every node links to its sources — fully reversible         │
   └──────────────────────────────│──────────────────────────────┘
   SQLite STORE ───────────────────▼─────────────────────────────┐
   │  every message, permanently — the source of truth           │
   └──────────────────────────────────────────────────────────────┘
```

The raw messages never leave the database, summaries are reversible, and the agent can
always recover the detail. In normal operation you never think about compaction again.

## Why lossless-hermes

| | |
|---|---|
| **Lossless by construction** | Every message is persisted; compaction only ever *adds* a summary layer. Drill-down is exact, not approximate. |
| **Recall lives in the engine** | The agent expands compacted history through first-class LCM tools during the active turn — not via a bolted-on, separate cross-session search step. |
| **The complete LCM feature port** | Full summary DAG, hybrid retrieval, entity coreference, tier-aware synthesis, and a full `/lcm` operator surface — the whole of LCM v4.1, not a subset. |
| **Drop-in for OpenClaw LCM users** | The on-disk SQLite schema is byte-compatible; an existing `lcm.db` migrates with [one command](#migrating-from-openclaw), no data loss. |
| **Degrades gracefully** | No `VOYAGE_API_KEY`? Retrieval runs FTS-only. `sqlite-vec` unavailable? The engine still runs. Optional dependencies stay optional. |
| **Built with provenance** | Every scar-tissue fix from LCM's 12 upstream audit waves is ported verbatim with `# LCM Wave-N` comments; decisions are recorded as [ADRs](./docs/adr/). |

### Versus Hermes built-in compression

Hermes core can persist conversation history in `state.db` before its built-in compression
rewrites the active prompt, and that pre-compression record may be recoverable later through
host-level history tools. `lossless-hermes` is different in *where recall lives*: it ships a
plugin-local store and summary DAG built specifically for drill-down, and exposes recall to
the agent **inside the active context engine** — current-session retrieval through LCM
tools, with explicit source-lineage and session-boundary rules. Position LCM on retrieval
quality and autonomous drill-down, not on the absence of any host-side history.

## Install

`lossless-hermes` is a Hermes plugin. Install Hermes first, then install the plugin.

### 1 · Install Hermes

Per [Hermes's install guide](https://github.com/NousResearch/hermes-agent#quick-install):

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

Hermes is not published to PyPI, so the plugin does **not** pin `hermes-agent` as a
dependency — operators install it separately.

### 2 · Install the plugin — directory mode (recommended)

Directory mode is the primary distribution model ([ADR-034](./docs/adr/034-plugin-distribution-directory-mode.md)).
The plugin is installed as a directory under `~/.hermes/plugins/`, which is where the
Hermes `plugins` CLI (`hermes plugins list` / `install` / `enable`) looks — so a
directory install is discoverable and manageable through that CLI.

Clone the repository directly into the Hermes plugins directory:

```bash
git clone https://github.com/electricsheephq/lossless-hermes \
  ~/.hermes/plugins/lossless-hermes
```

Then install the plugin's runtime dependencies into the **same Python environment Hermes
runs in** (a directory plugin has no pip dependency chain, so this step is explicit):

```bash
pip install 'httpx[socks]==0.28.1' sqlite-vec==0.1.9 pydantic==2.12.5 pyyaml==6.0.3 tenacity==9.1.4
```

Or, from an existing checkout, use the installer — it symlinks the checkout into
`~/.hermes/plugins/lossless-hermes/` and installs the pinned dependency set:

```bash
git clone https://github.com/electricsheephq/lossless-hermes
cd lossless-hermes
./scripts/install.sh
# Profile-scoped install:
HERMES_PROFILE=myprofile ./scripts/install.sh
# If Hermes runs in a venv, point the installer at its interpreter:
PYTHON=~/.hermes/.venv/bin/python ./scripts/install.sh
```

The Hermes directory loader reads [`plugin.yaml`](./plugin.yaml) at the package root to
discover the plugin. After installing, restart Hermes and run `hermes plugins list` — the
plugin appears as `lossless-hermes`.

### 2b · Install the plugin — pip / entry-point mode (secondary)

The plugin can also be installed as a pip package; it registers a
`hermes_agent.plugins` entry point that Hermes loads at startup.

```bash
# uv-managed environment
uv pip install lossless-hermes

# or with pip
pip install lossless-hermes
```

> **Note — a pip install is invisible to `hermes plugins list`.** The Hermes `plugins`
> CLI scans `~/.hermes/plugins/` (and the repo-bundled plugin directory); it does **not**
> enumerate `importlib.metadata` entry points. A pip-installed copy runs and works, but it
> will **not** appear in `hermes plugins list` and cannot be managed with
> `hermes plugins install` / `enable`. Use directory mode (above) if you want the plugin
> visible to that CLI. Do not install both ways at once in the same environment.

For development against a checkout, an editable pip install also works:

```bash
git clone https://github.com/electricsheephq/lossless-hermes
cd lossless-hermes
uv pip install -e ".[dev]"
```

## Quickstart

Enable the plugin and select LCM as the active context engine in `~/.hermes/config.yaml`.
**Both** settings are required — adding the plugin to `plugins.enabled` is not enough; LCM
must also win the `context.engine` selection:

```yaml
context:
  engine: lcm

plugins:
  enabled:
    - lossless-hermes
```

Start Hermes:

```bash
hermes
```

A startup log line confirms `lossless-hermes` is loaded and the context engine is `lcm`.
From the first turn, LCM ingests messages into its SQLite store, assembles context each
turn, and exposes the `/lcm` command surface. Run `/lcm help` for the full subcommand list,
or `/lcm status` to see the database path, summary counts, and health at a glance.

## Configuration

LCM reads configuration from the `lossless_hermes:` namespace in `~/.hermes/config.yaml`,
and from `LCM_*` environment variables. **Environment variables take precedence** when both
are set. Every setting has a sensible default — a zero-config install works.

```yaml
lossless_hermes:
  # ── Compaction ──────────────────────────────────────────────
  context_threshold: 0.75        # fraction of the window that triggers compaction
  fresh_tail_count: 64           # most-recent messages never compacted
  leaf_chunk_tokens: 20000       # source tokens per leaf-summary chunk
  incremental_max_depth: 1       # 0 = leaves only · 1 = one condensed pass · -1 = unlimited
  new_session_retain_depth: 2    # context retained after /new (-1 keeps everything)

  # ── Summarization model (falls back to the Hermes default model when unset) ──
  summary_model: "openai/gpt-5.4-mini"

  # ── Semantic + hybrid retrieval — optional; omit to run FTS-only ──
  voyage_api_key: "${VOYAGE_API_KEY}"

  # ── Scope control ──────────────────────────────────────────
  ignore_session_patterns:       # glob patterns: never store these sessions
    - "agent:*:cron:**"
  stateless_session_patterns: [] # glob patterns: may read LCM, never write to it
```

### Common settings

| YAML key (`lossless_hermes:`) | Environment variable | Default | Purpose |
|---|---|---|---|
| `enabled` | `LCM_ENABLED` | `true` | Enable / disable the plugin |
| `database_path` | `LCM_DATABASE_PATH` | `$HERMES_HOME/lossless-hermes/lcm.db` | SQLite database location |
| `context_threshold` | `LCM_CONTEXT_THRESHOLD` | `0.75` | Window fraction that triggers compaction (0.0–1.0) |
| `fresh_tail_count` | `LCM_FRESH_TAIL_COUNT` | `64` | Recent messages protected from compaction |
| `leaf_chunk_tokens` | `LCM_LEAF_CHUNK_TOKENS` | `20000` | Max source tokens per leaf-summary chunk |
| `incremental_max_depth` | `LCM_INCREMENTAL_MAX_DEPTH` | `1` | Condensation depth (`0` / `1` / `-1`) |
| `summary_model` | `LCM_SUMMARY_MODEL` | *(Hermes default)* | Model used for compaction summarization |
| `summary_provider` | `LCM_SUMMARY_PROVIDER` | *(Hermes default)* | Provider for summarization calls |
| `summary_timeout_ms` | `LCM_SUMMARY_TIMEOUT_MS` | `60000` | Per-call summarization timeout |
| `large_file_token_threshold` | `LCM_LARGE_FILE_TOKEN_THRESHOLD` | `25000` | File blocks above this size are externalized |
| `ignore_session_patterns` | `LCM_IGNORE_SESSION_PATTERNS` | *(none)* | Glob patterns for sessions to exclude from LCM |
| `stateless_session_patterns` | `LCM_STATELESS_SESSION_PATTERNS` | *(none)* | Glob patterns for read-only sessions |
| `voyage_api_key` | `VOYAGE_API_KEY` | *(unset)* | Enables semantic + hybrid retrieval |

> **Retrieval modes & `VOYAGE_API_KEY`.** Regex, full-text (FTS5), and verbatim search work
> with no external dependency. The `semantic` and `hybrid` modes of `lcm_grep` additionally
> require a `VOYAGE_API_KEY` for query embeddings; without one, those modes return a clear
> operator-facing error pointing back to the FTS modes.

## Agent tools

LCM registers **9 agent tools** so the model can search, recall, and self-diagnose:

| Tool | What it does |
|---|---|
| `lcm_grep` | Search history — `regex` / `full_text` (FTS5) / `verbatim` / `semantic` / `hybrid` (FTS + embeddings + rerank) modes; scope hits to leaf vs. condensed summaries |
| `lcm_describe` | Drill into a summary — its lineage plus a one-hop expansion to child summaries or raw messages |
| `lcm_expand` | DAG walker that expands a summary subtree back toward the original messages |
| `lcm_synthesize_around` | Fresh windowed synthesis — by calendar period (`"yesterday"`, `"last-7-days"`), by ±N hours around an anchor, or by top-K semantic similarity |
| `lcm_get_entity` | Look up a tracked entity in the coreference catalog |
| `lcm_search_entities` | Search / browse the entity catalog, optionally by entity type |
| `lcm_compact` | Operator-opt-in escape valve — let the agent trigger an LCM compaction pass |
| `lcm_status` | Read-only self-diagnosis — snapshot LCM's own health (config, counts, context pressure, cache state) mid-turn ([ADR-035](./docs/adr/035-lcm-status-doctor-model-tools.md)) |
| `lcm_doctor` | Read-only self-diagnosis — scan stored summaries for integrity problems (broken / fallback / truncated); does not repair ([ADR-035](./docs/adr/035-lcm-status-doctor-model-tools.md)) |

> `lcm_status` and `lcm_doctor` are read-only diagnostics — no DB writes, no owner gate. The
> `/lcm status` and `/lcm doctor` slash commands remain the surface for the write paths
> (`/lcm doctor apply`, etc.).
>
> `lcm_expand_query` (recursive expansion via a bounded sub-agent) is deferred to a future
> release — see [ADR-012](./docs/adr/012-subagent-defer.md).

## Operator commands

All operator commands are reachable as `/lcm <subcommand>` from within a Hermes session:

| Command | Purpose |
|---|---|
| `/lcm status` | Version, enablement, DB path & size, summary counts, health at a glance |
| `/lcm health` | Detailed subsystem health — embeddings, workers, synthesis cache, eval recall |
| `/lcm doctor` | Scan for broken / truncated summaries; `doctor apply` repairs, `doctor clean` removes |
| `/lcm purge` | Soft-suppress leaves matching criteria (dry-run by default) |
| `/lcm reconcile-session-keys` | Merge legacy session keys into one logical session |
| `/lcm worker` | Inspect background-worker state, or force an embedding-backfill tick |
| `/lcm backup` | Timestamped backup of the LCM SQLite database |
| `/lcm rotate` | Back up the database, clear the assemble-snapshot cache, WAL-checkpoint, and stamp the last-rotate time |
| `/lcm help` | List every subcommand |

> The recall + drift **evaluation harness** ships in `src/lossless_hermes/eval/` and runs
> via the `live-eval` CI workflow; the `/lcm eval` slash-command wiring is not yet landed.

## Migrating from OpenClaw

Already running LCM on OpenClaw? Your conversation history comes with you. The on-disk
SQLite schema is byte-compatible (per [ADR-025](./docs/adr/025-openclaw-migration.md) and
[spike 003](./docs/spike-results/003-identity-hash.md) — message `identity_hash` values do
not diverge across the port):

```bash
cp ~/.openclaw/lcm.db "$HERMES_HOME/lossless-hermes/lcm.db"
lossless-hermes import-openclaw
```

`import-openclaw` runs the migration ladder idempotently, refuses to overwrite an existing
destination without `--force`, and sample-validates `identity_hash` on migrated rows. See
`lossless-hermes import-openclaw --help` for `--from`, `--to`, `--force`, `--validate-rows`,
and `--dry-run`.

## Platform support

| Platform | Python | Status |
|---|---|---|
| macOS arm64 / x86_64 | Homebrew · python.org · uv — **3.12 recommended** | Supported |
| Linux x86_64 / arm64 | system · deadsnakes · uv · Alpine | Supported |
| Windows | via **WSL2** (Ubuntu Python) | Supported |
| Windows (native) | — | Out of scope — use WSL2 |

> **Apple `/usr/bin/python3` is unsupported.** It is Python 3.9.6 (below the `>=3.11` floor)
> and its `sqlite3` is built without `enable_load_extension`, which blocks `sqlite-vec`. Fix:
> `brew install python@3.12` (or `uv python install 3.12`) and install against that
> interpreter. See [ADR-004](./docs/adr/004-sqlite3-backend.md) and
> [ADR-005](./docs/adr/005-python-version.md).

## Status & maturity

**Current release: [v0.1.1](https://github.com/electricsheephq/lossless-hermes/releases).**

`lossless-hermes` is a **feature-complete** port of LCM v4.1 — all 122 planned port issues
are merged, and CI is green across `{macOS, ubuntu} × {Python 3.11, 3.12, 3.13}` with
~4,000 tests. It is **early**: production exposure is still limited, and the project is in an
active hardening phase. An ongoing architecture review against an independent, production
LCM implementation feeds correctness fixes back in — v0.1.1 shipped two such fixes, and
further findings are tracked openly in the
[issue tracker](https://github.com/electricsheephq/lossless-hermes/issues).

Recommended use today: evaluation, development, and non-critical workloads. Watch the
[releases](https://github.com/electricsheephq/lossless-hermes/releases) and
[`CHANGELOG.md`](./CHANGELOG.md) as hardening continues.

A few capabilities are gated on operator-provided resources rather than shipped on by
default — the live Voyage recall benchmark requires a `VOYAGE_API_KEY`, and a full
integration soak is recommended before production rollout. These are documented in
[`BLOCKERS.md`](./BLOCKERS.md).

## Architecture

LCM layers cleanly onto Hermes's `ContextEngine` plugin slot:

- **Storage** — a SQLite schema with an idempotent migration ladder, FTS5 + trigram search,
  and optional `sqlite-vec` vector tables. Byte-compatible with OpenClaw LCM databases.
- **Engine** — `LCMEngine` implements the `ContextEngine` ABC; per-turn ingest and context
  assembly run through Hermes's `pre_llm_call` / `post_llm_call` hooks.
- **Compaction** — leaf and condensed summary passes build the DAG, with anti-thrashing
  guards and a synthesis circuit breaker.
- **Retrieval** — FTS5 / trigram search, plus optional Voyage embeddings + `sqlite-vec`
  hybrid search (reciprocal-rank fusion + rerank) when a key is configured.
- **Entities & synthesis** — an entity-coreference pipeline and tier-aware synthesis
  dispatch with a result cache.
- **Operator surface** — the `/lcm` command family and the `import-openclaw` CLI.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full system map, and
[`docs/adr/`](./docs/adr/) for the decision record behind every load-bearing choice.

### Naming convention

Three forms of the name appear, deliberately distinct:

| Form | Where it appears |
|---|---|
| `lossless-hermes` (hyphenated) | PyPI distribution name; `pip install` / `pip uninstall` |
| `lossless_hermes` (snake_case) | Python module — `import lossless_hermes` |
| `lossless_hermes:` (YAML key) | the configuration namespace in `~/.hermes/config.yaml` |

## Related work

- **[Lossless Claw](https://github.com/Martian-Engineering/lossless-claw)** — the upstream
  LCM v4.1 implementation for OpenClaw (TypeScript) that `lossless-hermes` is a faithful port
  of. The canonical source of the design.
- **[hermes-lcm](https://github.com/stephenschoettler/hermes-lcm)** — an independent,
  currently-shipping LCM-for-Hermes plugin built from the LCM paper. A leaner sibling
  implementation; `lossless-hermes` tracks its production experience as part of an ongoing
  architecture review (see the
  [issue tracker](https://github.com/electricsheephq/lossless-hermes/issues)).
- **[The LCM paper](https://papers.voltropy.com/LCM)** — Ehrlich & Blackman, Voltropy PBC —
  the design both implementations descend from.

## Documentation

| Document | Purpose |
|---|---|
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | System architecture, data flow, target layout |
| [`CHANGELOG.md`](./CHANGELOG.md) | Per-release change history |
| [`docs/adr/`](./docs/adr/) | Architecture Decision Records — numbered, dated, status-tagged |
| [`docs/porting-guides/`](./docs/porting-guides/) | Per-subsystem TS → Python porting guides |
| [`docs/reference/`](./docs/reference/) | Dependency matrix, Hermes-hook reference, source map |
| [`docs/spike-results/`](./docs/spike-results/) | De-risking spike findings |
| [`CONTRIBUTING.md`](./CONTRIBUTING.md) | Dev setup, test policy, lint / format, branch policy |
| [`STATUS.md`](./STATUS.md) · [`BLOCKERS.md`](./BLOCKERS.md) | Live project state and open decisions |

## Contributing

Contributions are welcome. The development loop, test policy, and lint/format gates are in
[`CONTRIBUTING.md`](./CONTRIBUTING.md). In short: `uv pip install -e ".[dev]"`, work on a
branch, keep the CI matrix green, and open a PR. Pre-commit hooks (`ruff`, `ty`,
file-hygiene) must pass.

## License

[MIT](./LICENSE) © Electric Sheep HQ.
