# Python Dependencies Reference

**Python version target:** `>=3.11` (matches `hermes-agent`'s `requires-python = ">=3.11"` — see `/Volumes/LEXAR/Claude/hermes-agent/pyproject.toml` line 10).
**Confidence target:** 95%
**Pinning policy:** **Exact pins** (`==X.Y.Z`) on every direct runtime dependency. We mirror Hermes's policy verbatim — see Hermes `pyproject.toml` lines 14-39 for the rationale, but in short: ranges let PyPI ship a fresh transitive into our installs without a review on our side, and the 2026-05-12 Mini Shai-Hulud worm hitting `mistralai 2.4.6` is the canonical case study. Bump pins intentionally; regenerate `uv.lock` on every bump.

## Runtime dependencies

### Core (every install)

| Package | Version pin | License | Purpose | Alternatives considered | Why this one |
|---|---|---|---|---|---|
| `httpx[socks]` | `==0.28.1` | BSD-3-Clause | Voyage AI HTTP client (POST `/v1/embeddings`, `/v1/rerank`) | `aiohttp`, `requests`, `urllib3` | Async-first + sync from one client; `Timeout` + `Retry-After` parsing map 1:1 from the TS `fetch`+`AbortController` recipe (see spike 004 §"Mapping table"). **Already a Hermes core dep at this exact version** — sharing the pin keeps our resolution coherent with the host and lets `uv pip install lossless-hermes hermes-agent` finish without dependency-resolver pain. `[socks]` extra inherited from Hermes (cost: one optional transitive, benefit: pin parity). |
| `sqlite-vec` | `==0.1.9` | Apache-2.0 OR MIT | `vec0` virtual-table KNN over float32 embeddings | `apsw` + manual extension load, `chromadb`, `faiss`, `pgvector` (different DB), `lancedb` | Single SQLite file holds both `vec0` and `fts5` — matches LCM's "one connection, one file" design. Maintained by Alex Garcia (same author as Node `sqlite-vec` bindings — bug-for-bug parity with the TS source). PyPI ships `py3-none-*` wheels (pure-Python loader; native code is inside `libsqlite_vec.dylib/.so` bundled in the wheel) for `macosx_11_0_arm64`, `manylinux2014_x86_64`, `manylinux2014_aarch64` for Python 3.10-3.14. **Confirmed working on stdlib `sqlite3`** for both `vec0` and trigram-FTS5 in the same connection — see [spike 001](../spike-results/001-sqlite-vec-python.md) §Findings and [spike 005](../spike-results/005-sqlite3-fts5-trigram.md) §"Coordination with sqlite-vec". |
| `pydantic` | `==2.12.5` | MIT | Runtime validation of config (`config.yaml`), doctor contract output, Voyage response payloads | `dataclasses`, `attrs`, `msgspec`, `marshmallow` | Runtime validation needed — config is operator-supplied and Voyage responses are external. Pydantic v2's Rust core is fast enough to validate every embed response without throttling. **Already a Hermes core dep at this exact version** — avoids dependency-resolver thrash. |
| `pyyaml` | `==6.0.3` | MIT | Load `~/.hermes/lossless/config.yaml` (operator-tunable knobs) | `ruamel.yaml`, `tomllib` (stdlib, TOML-only), `json5` | Hermes already standardizes on YAML for plugin config (`hermes_cli/config.py`). **Already a Hermes core dep at this exact version.** TOML would require operators to learn a second syntax. |
| `tenacity` | `==9.1.4` | Apache-2.0 | Optional retry helpers for migration/backfill routines outside the Voyage hot path | Hand-rolled `while attempt < max: ...` loops | The Voyage retry loop is **not** delegated to tenacity — it's hand-coded per spike 004 because the retry contract is load-bearing (Wave-1 cap-at-25s, Wave-7 PII-suppression, lock-budget-aware 60s gate). Tenacity is used only for non-load-bearing retries (e.g. migration tooling reconnecting after `database is locked`). **Already a Hermes core dep at this exact version** — adopting it costs zero install footprint. |

### Host runtime (NOT a dependency — installed separately)

| Package | Status | Why not pinned |
|---|---|---|
| `hermes-agent` | **Host, not dep** | The operator installs Hermes via `curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \| bash` (or `uv pip install -e ".[all,dev]"` from the cloned source — see `/Volumes/LEXAR/Claude/hermes-agent/README.md`). Hermes is **not on PyPI** as of 2026-05-13. We declare the plugin entry point via `[project.entry-points."hermes_agent.plugins"]` (the canonical group, confirmed at `hermes_cli/plugins.py:170`), but adding `hermes-agent` as a `dependencies` entry would be a lie — pip cannot fetch it, and `uv lock` would fail. **Decision: ADR-005 (pending) — see "Open decisions" below.** |

### Conditional / optional fallbacks

| Package | Version pin | License | Condition | Purpose | Notes |
|---|---|---|---|---|---|
| `apsw` | `==3.53.1.0` | Zlib | `[apsw]` extra (opt-in only) | Alternate SQLite driver if stdlib `sqlite3` lacks `enable_load_extension` (e.g. some custom Python builds, packagers that disabled `--enable-loadable-sqlite-extensions`) | Cross-platform wheels: `macosx_11_0_arm64`, `macosx_10_9_x86_64`, `manylinux2014_*` for Python 3.10-3.13. **API is NOT PEP-249** — swap is a connection-layer rewrite, not a drop-in. Keep `open_lcm_db()` isolated as a single function. See [spike 001](../spike-results/001-sqlite-vec-python.md) §Findings → "apsw on macOS". |
| `pysqlite3-binary` | **NOT pinned, NOT recommended** | Zlib | — | (Would bundle a newer SQLite than stdlib) | **No macOS wheels exist** (latest `0.5.4.post2` is `manylinux2014_x86_64`-only — confirmed in spike 001). Adding as a hard dep breaks every Mac contributor. We don't need a newer SQLite anyway — Homebrew Python 3.12/3.13/3.14 ship 3.53.0, well above the FTS5-trigram floor (3.34). If a future need arises for a newer SQLite, prefer `apsw` (cross-platform) over `pysqlite3-binary` (Linux-only). |
| `respx` | `==0.21.1` | BSD-3-Clause | `[dev]` extra | Mock `httpx` for Voyage client unit tests (24 fixtures per spike 004 §"Test fixtures") | Designed by the encode/ team (httpx maintainers) — the canonical `httpx` mock. Used to port `lossless-claw/test/voyage-client.test.ts` (561 LOC) fixture-for-fixture. |

## Dev / test dependencies (`[dev]` extra)

| Package | Version pin | License | Purpose |
|---|---|---|---|
| `pytest` | `==9.0.2` | MIT | Test runner. **Matches Hermes's pin** for plugin-host coherence. |
| `pytest-asyncio` | `==1.3.0` | Apache-2.0 | Async test support (`asyncio_mode = "auto"`). **Matches Hermes's pin.** |
| `pytest-mock` | `==3.14.0` | MIT | `mocker` fixture for SQLite + filesystem mocks. |
| `pytest-cov` | `==6.0.0` | MIT | Coverage gate in CI (target: 90%+ for core, 80%+ overall). |
| `respx` | `==0.21.1` | BSD-3-Clause | `httpx` mock router for Voyage client tests (see Conditional table). |
| `ruff` | `==0.15.10` | MIT | Lint + format. **Matches Hermes's pin.** Adopt Hermes's `select = ["PLW1514"]` rule (explicit `encoding=` on `open()` — Windows cp1252 bites otherwise; see Hermes `pyproject.toml` lines 257-275). |
| `ty` | `==0.0.21` | MIT | Type checker (Astral, sibling of ruff — faster than mypy, **matches Hermes's choice**). Use `ty.environment.python-version = "3.13"` to match Hermes. |
| `mypy` | `==1.13.0` | MIT | Backup type checker — opt-in only via `[type]` extra, NOT in `[dev]`. `ty` is the primary; `mypy` is for IDE plugins and legacy CI integrations that don't speak ty yet. |
| `pre-commit` | `==4.0.1` | MIT | Git hooks (`ruff check`, `ruff format --check`, `ty check`). |

**Decision: ty over mypy/pyright.** Rationale: Hermes already runs `ty` in CI (see `[tool.ty.environment]` in Hermes `pyproject.toml` lines 244-251). Using the same type-checker means our error messages and rule semantics align with the host. `mypy` is offered as an opt-in fallback for environments where ty isn't available. Pyright is omitted to avoid a third type-checker — if a contributor uses VS Code, the Pyright extension can still consume `ty`'s `pyproject.toml` config without us pinning Pyright as a dep.

## pyproject.toml skeleton

```toml
[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"

[project]
name = "lossless-hermes"
version = "0.1.0"
description = "Lossless Context Management plugin for hermes-agent"
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
authors = [{ name = "Electric Sheep HQ" }]

# Exact pins on every direct runtime dep (see Hermes pyproject.toml lines 14-39
# for the canonical rationale — same supply-chain risk model applies here).
# When updating: bump the version below AND regenerate uv.lock with `uv lock`.
dependencies = [
    "httpx[socks]==0.28.1",      # Voyage HTTP — matches Hermes pin
    "sqlite-vec==0.1.9",          # vec0 KNN — see spike 001
    "pydantic==2.12.5",           # Config + Voyage response validation — matches Hermes pin
    "pyyaml==6.0.3",              # config.yaml loading — matches Hermes pin
    "tenacity==9.1.4",            # Non-hot-path retries — matches Hermes pin
]

[project.optional-dependencies]
# Cross-platform SQLite fallback. Stdlib sqlite3 covers 100% of mainstream
# Python builds (per spike 001 + 005); apsw is the documented fallback only.
apsw = ["apsw==3.53.1.0"]

# Dev/test surface. Pinned to Hermes's versions where overlap exists so
# plugin authors can run Hermes's test suite alongside ours without conflict.
dev = [
    "pytest==9.0.2",              # matches Hermes pin
    "pytest-asyncio==1.3.0",      # matches Hermes pin
    "pytest-mock==3.14.0",
    "pytest-cov==6.0.0",
    "respx==0.21.1",              # httpx mock router for Voyage tests
    "ruff==0.15.10",              # matches Hermes pin
    "ty==0.0.21",                 # matches Hermes pin
    "pre-commit==4.0.1",
]
# Opt-in: legacy type-check via mypy for IDE plugins that don't yet speak ty.
type-mypy = ["mypy==1.13.0"]

[project.entry-points."hermes_agent.plugins"]
# Entry point group is canonical — see hermes_cli/plugins.py line 170:
#   ENTRY_POINTS_GROUP = "hermes_agent.plugins"
# The Hermes plugin loader iterates entries in this group at startup,
# imports each callable, and invokes it with a PluginContext. The plugin's
# register() then calls ctx.register_context_engine(LosslessEngine()).
lossless-hermes = "lossless_hermes:register"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "integration: marks tests requiring VOYAGE_API_KEY (skipped by default)",
    "live: marks tests that hit production Voyage (nightly only)",
]
addopts = "-m 'not integration and not live'"

[tool.ruff]
line-length = 100
target-version = "py311"
preview = true  # mirrors Hermes — needed for PLW1514

[tool.ruff.lint]
# At minimum, inherit Hermes's load-bearing rule (explicit encoding= on open()).
# We can add more rules in a follow-up; starting with parity is safest.
select = ["PLW1514"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["PLW1514"]

[tool.ty.environment]
python-version = "3.13"

[tool.ty.rules]
unknown-argument = "warn"
redundant-cast = "ignore"

[tool.coverage.run]
source = ["lossless_hermes"]
branch = true

[tool.coverage.report]
fail_under = 80
show_missing = true
```

## Platform support matrix

Results from [spike 001](../spike-results/001-sqlite-vec-python.md) §Findings and [spike 005](../spike-results/005-sqlite3-fts5-trigram.md) §"SQLite versions found". Cells annotated **(tested)** were exercised first-hand; **(inferred)** is strong inference from PyPI wheel availability + CPython build defaults but not locally run.

| Platform | Python source | stdlib `sqlite3` loads `vec0`? | FTS5 + trigram? | `apsw` needed? | Notes |
|---|---|---|---|---|---|
| macOS arm64 | Homebrew `python@3.12` (3.12.13) | YES **(tested)** | YES **(tested, SQLite 3.53.0)** | No | Default recommendation. |
| macOS arm64 | Homebrew `python@3.13` (3.13.12) | YES **(inferred from spike 005)** | YES **(tested, SQLite 3.53.0)** | No | Same code path as 3.12. |
| macOS arm64 | Homebrew `python@3.14` (3.14.3) | YES **(tested)** | YES **(tested, SQLite 3.53.0)** | No | |
| macOS arm64 | `/usr/bin/python3` (Apple, 3.9.6) | **NO** — `AttributeError: enable_load_extension` **(tested)** | YES (FTS5 fine — SQLite 3.51.0) but irrelevant | No (apsw won't help here — Python is below our floor anyway) | **Below `requires-python` floor (3.11+).** Document loudly in CONTRIBUTING that Apple system Python is not supported. |
| macOS arm64 | python.org installer | YES **(inferred — CPython build script enables `--enable-loadable-sqlite-extensions`)** | YES **(inferred — `--enable-fts5` is CPython default since 3.7)** | No | Not locally verified. Recommend adding a macos-arm64 + python.org install job to the CI matrix in the first PR. |
| macOS x86_64 | Homebrew | YES **(inferred from wheel coverage + same build flags)** | YES **(inferred)** | No | Both `sqlite-vec` (`py3-none-*`) and `apsw` (`macosx_10_9_x86_64`) ship wheels. |
| Linux x86_64 | python.org/deadsnakes/system | YES **(inferred — every mainstream Linux Python build enables loadable extensions)** | YES **(inferred — Hermes itself runs FTS5+trigram on stdlib sqlite3 in production on Linux; see spike 005 §Method step 7)** | No | Close the inference gap by adding `ubuntu-latest` to the CI matrix in the first PR. |
| Linux arm64 | system | YES **(inferred from `manylinux2014_aarch64` wheel availability)** | YES **(inferred)** | No | |
| Linux musl/Alpine | `python:3.13-alpine` | YES **(inferred — alpine ships SQLite 3.45+)** | YES **(inferred)** | No | Add to CI matrix if Alpine deployment is a target. |
| Windows native | python.org installer | **Unknown — out of scope for v0.1** | YES (probably — CPython defaults) | Probably | Hermes README explicitly tags native Windows as "early beta"; LCM follows suit. Document "use WSL2" until first-hand testing. |
| Windows WSL2 | Ubuntu Python | Same as Linux x86_64 | Same as Linux x86_64 | No | Recommended Windows path. |

**Action item:** First PR should add a GitHub Actions matrix covering `{ubuntu-latest, macos-latest} × {python-3.11, python-3.12, python-3.13}` to convert every "inferred" cell above into "tested".

## Voyage credentials

LCM v4.1 supports three Voyage-key resolution paths, in priority order. We replicate the OpenClaw layout under `$HERMES_HOME/lossless/` so an operator who already has an OpenClaw `~/.openclaw/credentials/voyage-api-key` only needs to copy it to the Hermes equivalent (or symlink).

| Source | Path | Priority | Notes |
|---|---|---|---|
| Config-file inline | `context.lcm.voyage_api_key` in `~/.hermes/lossless/config.yaml` | **Highest** | Supports `${VOYAGE_API_KEY}` env-var interpolation à la Hermes's existing config patterns. Useful when an operator wants to commit the config skeleton but inject the secret from a vault at deploy time. |
| Env var | `VOYAGE_API_KEY` | Middle | Most common path for CI and one-off shell invocations. Matches OpenClaw's primary env-var name verbatim. |
| File | `$HERMES_HOME/lossless/credentials/voyage-api-key` (default `$HERMES_HOME = ~/.hermes`) | Lowest | Mirrors OpenClaw's `~/.openclaw/credentials/voyage-api-key` layout (see [spike 004](../spike-results/004-voyage-python-client.md) §"Python implementation sketch" `_load_api_key`). One-file-per-secret keeps the surface auditable — `chmod 600` enforceable. |

Resolution order is implemented in `lossless_hermes.config.resolve_voyage_key()` and tested explicitly (config inline > env > file > raise `VoyageError(kind="auth")`).

## Open decisions

These belong in their own ADRs before Phase 2 closes. Each is at 90%+ confidence but the formal record is missing.

- **ADR-005 (pending): Pin `hermes-agent` as a dependency or not?**
  - **Current direction:** Do **NOT** pin. Hermes is not on PyPI (verified 2026-05-13 — README shows curl/uv-install only); declaring it in `dependencies` would make `uv lock` fail. Plugin discovery via the `hermes_agent.plugins` entry-point group works regardless of how Hermes is installed (entry points are scanned by Hermes at startup from `importlib.metadata.entry_points()` — see `hermes_cli/plugins.py:1043`).
  - **Alternative kept open:** If Hermes ships to PyPI in the future, add `hermes-agent>=0.13.0,<0.14` (range, not exact pin — Hermes major-versions are infrequent and the ABC is stable; a range here aligns us with semver expectations on the host side).
  - **Risk:** Without a hard pin, `pip install lossless-hermes` in a Hermes-less environment silently succeeds and only fails at runtime when the entry point can't be loaded. Mitigation: a startup health-check in `lossless_hermes/__init__.py` that imports `agent.context_engine` and emits an actionable error if Hermes isn't on the path.

- **ADR-006 (pending): `apsw` as a hard dep, conditional, or fallback-only?**
  - **Current direction:** `apsw` as a `[apsw]` optional extra (NOT in core, NOT in `[dev]`). 100% of supported platforms work on stdlib `sqlite3` per spikes 001 + 005. `apsw` exists only for the niche of custom-compiled Python builds without `--enable-loadable-sqlite-extensions`. Keep `open_lcm_db()` as a single function with a stdlib/apsw switch driven by a config flag — the swap is non-trivial because apsw's API is not PEP-249, so the abstraction has to be designed in from day one.
  - **Risk:** If we hit a packager (Nix, AUR, Homebrew) whose default Python disables loadable extensions, opt-in extras break. Mitigation: document the failure mode in CONTRIBUTING + provide a one-line install instruction (`uv pip install lossless-hermes[apsw]`).

- **ADR-007 (pending): `ty` vs `mypy` vs both?**
  - **Current direction:** `ty` (Astral) as primary, `mypy` as opt-in `[type-mypy]` extra. Reason: Hermes already uses `ty` in CI and the rule semantics align. Pyright is omitted (VS Code's Pyright extension consumes our `pyproject.toml` config regardless).
  - **Risk:** `ty` is at `0.0.21` — pre-1.0, API may shift. Mitigation: pin exactly; bump deliberately; the `mypy` fallback covers any breakage window.

## Remaining 5% risk (per dependency)

| Risk | Severity | Mitigation |
|---|---|---|
| `sqlite-vec` is still 0.1.x; a minor bump could break the `vec0` SQL surface | medium | Exact-pin (`==0.1.9`); CI runs every fixture on bump; budget one upgrade cycle per minor release. Same author maintains the Node binding, so cross-runtime parity is incentive-aligned. |
| `apsw` API divergence from stdlib `sqlite3` (no PEP-249) | medium | Connection-open logic isolated behind `open_lcm_db()`; the apsw path is opt-in and exercised by a dedicated CI lane on bump. |
| Hermes upstream removes / renames `hermes_agent.plugins` entry-point group | low | Entry-point group name is hard-coded in Hermes for years; pin Hermes to a known-good version range when (if) we add it as a dep. Health-check at import time gives actionable error. |
| Voyage SDK released to PyPI later, eclipsing our hand-rolled `httpx` client | low | Our client encodes 7+ production-hardened wave fixes (PII suppression, lock-budget-aware Retry-After, dim-mismatch sentinels, output_dimension forwarding) — see [spike 004](../spike-results/004-voyage-python-client.md) §"Retry/backoff rules". A first-party SDK would have to ship the equivalent before we'd consider swapping. Until then, hand-rolled wins. |
| `ty` reaches 0.1.0 / 1.0.0 with breaking config changes | low | Exact-pin; bump deliberately; `mypy` fallback covers any breakage window. |
| python.org Python installer not locally validated | low | Spike 001 + spike 005 §"Remaining 5% risk" both flag this. CI matrix addition is the close-out plan. |
| Native Windows (non-WSL2) loadable-extension support | medium | Out of scope for v0.1; track via WSL2 as the supported Windows path. Document the limitation. Hermes itself tags native Windows as "early beta". |
| Linux not first-hand tested in spikes 001/005 | low | CI matrix (`ubuntu-latest`) added in first PR closes this. Hermes runs FTS5+trigram on stdlib sqlite3 in production on Linux, which is strong corroboration. |
| Supply-chain attack on a transitive dep (Mini Shai-Hulud style) | medium | Exact pins on direct deps + `uv.lock` checked into the repo. `uv sync --locked` in CI fails closed if a transitive shifts. Inherits the same posture Hermes adopted on 2026-05-12. |
| `httpx` 0.28 → 0.29 minor bump changes `Timeout` or `Retry-After` semantics | low | Exact-pin `==0.28.1`. Spike 004's mapping table is 0.28-specific. Bump deliberately; re-run the 24 fixture tests. |
