# Contributing to lossless-hermes

Thanks for picking up an issue. This document covers the dev loop, the gates a PR has to pass, and the conventions the repo enforces. If you find a gap between what this file says and what `pre-commit run --all-files` or CI does, **CI is the source of truth** — fix this file in the same PR.

## TL;DR

```bash
git clone https://github.com/electricsheephq/lossless-hermes
cd lossless-hermes
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pre-commit install

pytest -m 'not live'      # default test suite (skips live/integration)
ruff check . && ruff format --check .
ty check src/             # type-check src/ only
```

## Prerequisites

- **Python 3.11+** (3.12 recommended per [ADR-005 §Decision](./docs/adr/005-python-version.md#decision)). Homebrew, pyenv, or uv-managed.
- **[uv](https://github.com/astral-sh/uv)** — recommended package manager. Plain `pip` + `venv` works too, but `uv` matches our `uv.lock` workflow.
- A working **Hermes** install in the same Python env if you're running integration tests (see [ADR-007](./docs/adr/007-hermes-as-dependency.md)). For pure unit tests on the LCM side, Hermes is not required at install time.

> [!WARNING]
> **Apple `/usr/bin/python3` is UNSUPPORTED.** It is Python 3.9.6 (below the `>=3.11` floor) and its `sqlite3` module is built **without** `enable_load_extension`, so `sqlite-vec` cannot load. See [ADR-004 §Consequences](./docs/adr/004-sqlite3-backend.md#consequences) and [ADR-005 §Consequences](./docs/adr/005-python-version.md#consequences).
>
> **One-line fix:** `brew install python@3.12` (or `uv python install 3.12`), then `uv venv --python 3.12` and re-run the install steps against the new interpreter.

## Setup

```bash
git clone https://github.com/electricsheephq/lossless-hermes
cd lossless-hermes

# Create and activate a virtual environment
uv venv
source .venv/bin/activate   # or: source .venv/bin/activate.fish, .venv\Scripts\activate (Windows)

# Install the package in editable mode + dev extras
uv pip install -e ".[dev]"

# Install the pre-commit hook into .git/hooks
pre-commit install
```

The `[dev]` extra (see [`pyproject.toml`](./pyproject.toml)) pulls in everything you need to run the test suite, lint, format, and type-check.

### Optional extras

- `uv pip install -e ".[dev,apsw]"` — adds the `apsw` SQLite driver fallback. Only needed if your Python build was compiled without `--enable-loadable-sqlite-extensions` (rare; see [ADR-004](./docs/adr/004-sqlite3-backend.md)).
- `uv pip install -e ".[dev,type-mypy]"` — adds `mypy` as a backup type-checker. `ty` is the primary gate (see [Type checking](#type-checking)). Use this only if your IDE plugin doesn't speak `ty` yet.

## Test policy

Run the **default** suite for every PR:

```bash
pytest -m 'not live'
```

The test markers (defined in [`pyproject.toml`](./pyproject.toml) `[tool.pytest.ini_options]`):

| Marker | Default? | What it gates | When CI runs it |
|---|---|---|---|
| (no marker) | YES | Pure unit tests with no network or API key | Every push, every PR |
| `integration` | NO (skipped by `addopts`) | Tests that hit a local SQLite + filesystem | On request; will gain a dedicated CI lane in Epic 01 |
| `live` | NO (skipped by `addopts`) | Tests that call production Voyage AI | Nightly on `main` only |

`addopts = "-m 'not integration and not live'"` in `pyproject.toml` is what makes the default skip both — bare `pytest` is equivalent to `pytest -m 'not live'`.

**`live` tests require API keys** (`VOYAGE_API_KEY` at minimum). CI runs them on `main` only, never on PR pushes. If you're adding a new live test, mark it `@pytest.mark.live` and document the key it needs in the test docstring.

## Type checking

The primary type-checker is **[ty](https://github.com/astral-sh/ty)** (Astral, sibling of ruff). Per [ADR-008](./docs/adr/008-typechecker.md), we use the same tool Hermes uses so error messages and rule semantics align with the host.

```bash
ty check src/
```

Note: `ty` only checks `src/`. Test code is allowed to be loose per ADR-008 §Consequences. CI enforces `ty check src/` as a gate.

`ty` is pre-1.0 (`0.0.21` at time of writing) — pin bumps are deliberate (see [Dependency bumps](#dependency-bumps)).

### mypy as a fallback

If you use an IDE that doesn't yet support `ty` (e.g., a Pyright/mypy-only setup), install the `[type-mypy]` extra:

```bash
uv pip install -e ".[dev,type-mypy]"
mypy src/
```

`mypy` is an **opt-in fallback**, not the CI gate. Disagreements between `ty` and `mypy` are resolved in favor of `ty`.

## Lint + format

Lint and format both go through **[ruff](https://github.com/astral-sh/ruff)**, pinned to Hermes's version. Config lives in [`pyproject.toml`](./pyproject.toml) under `[tool.ruff]`.

```bash
ruff check .              # lint
ruff format --check .     # check formatting (no rewrites)
ruff format .             # apply formatting
```

The load-bearing rule today is `PLW1514` (explicit `encoding=` on `open()`) — same as Hermes. Tests are exempt via `[tool.ruff.lint.per-file-ignores]`.

## Commit hooks

`pre-commit install` (run once) sets up the `.pre-commit-config.yaml` gate (landing with [issue 00-03](./epics/00-scaffolding/issues/00-03-precommit-hooks.md)). On every `git commit`, it runs:

1. File-hygiene checks (trailing whitespace, EOF newlines, YAML/TOML parse-cleanliness, large-file guard, LF line endings)
2. `ruff check --fix` + `ruff format`
3. `ty check src/` (as a `language: system` local hook, since `ty` has no upstream pre-commit repo as of 2026-05-13)

To run on every file manually:

```bash
pre-commit run --all-files
```

**Don't use `--no-verify`** to skip hooks. If a hook fails, fix the issue and re-commit — the hooks are the same checks CI runs, so bypassing them locally just moves the failure to CI. If a hook is genuinely broken, open an issue.

CI does **not** re-run pre-commit (per [issue 00-03 §AC](./epics/00-scaffolding/issues/00-03-precommit-hooks.md)); it runs the underlying `ruff check`, `ruff format --check`, and `ty check` directly. Same coverage, no double-paying for the same gate.

## Branch + PR policy

- **Branch naming:** `port/<issue-id>-<short-slug>` for porting work (e.g. `port/00-08-readme-and-docs`). Other prefixes: `fix/`, `chore/`, `docs/`, `spike/`.
- **One issue per PR.** Issues are scoped to be PR-sized; if you're tempted to combine, split.
- **PR title:** `[<issue-id>] <area>: <imperative-verb-phrase>` — e.g. `[00-08] docs: README install/quickstart blocks + project nav`.
- **PR description** must reference the issue spec and tick the AC checklist (`- [x]` for each item) before review.
- **Issue spec template:** [`.github/ISSUE_TEMPLATE/port-issue.md`](./.github/ISSUE_TEMPLATE/port-issue.md). Use it when filing new issues; the executor pipeline reads it.

CI gates a PR can pass with:

- All default tests green (`pytest -m 'not live'`)
- `ruff check` + `ruff format --check` clean
- `ty check src/` clean
- Coverage gate met (target: ≥ 80% overall, ≥ 90% on core — see [`pyproject.toml`](./pyproject.toml) `[tool.coverage.report]`)

A reviewer's role is to confirm: AC checklist ticked, no scope creep beyond the issue, code matches the porting-guide direction (where one exists), tests cover the new surface, and ADR references are accurate.

## Dependency bumps

Per [ADR-006](./docs/adr/006-dependency-pinning.md), every direct runtime dependency is **exact-pinned** (`==X.Y.Z`). Bumps go through a deliberate PR — not a passive `uv sync` drift.

Workflow:

1. Edit the pin in [`pyproject.toml`](./pyproject.toml).
2. Run `uv lock` to regenerate `uv.lock`.
3. Commit `pyproject.toml` and `uv.lock` together in one commit.
4. PR title: `[deps] bump <package> X.Y.Z → X.Y.Z+1`.
5. PR body: link release notes, call out any user-visible behavior change, confirm the test suite passes locally.

For optional / `[dev]` extras, the same workflow applies — exact pins on every entry, lock regenerated, bumped in a dedicated PR.

## Source layout (Phase 2)

```
src/lossless_hermes/      # the package
tests/                    # pytest (mirrors src/ layout)
docs/                     # ADRs, porting guides, spike results, references
epics/                    # 10 epics, each with per-issue specs
scripts/                  # build / migration helpers (e.g. schema_diff.sh)
.github/                  # workflows, issue templates
```

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the target module structure.

## Where to ask questions

- Open an issue on this repo for porting-process or scaffolding questions.
- For upstream Hermes questions (hook contracts, plugin loader), file under [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/issues) and cross-link.
- For upstream LCM (TS source) questions, file under [Martian-Engineering/lossless-claw](https://github.com/Martian-Engineering/lossless-claw/issues).

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](./LICENSE).
