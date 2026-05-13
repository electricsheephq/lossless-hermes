---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-00] scaffolding: GitHub Actions CI matrix ({macOS, ubuntu} × {3.11, 3.12, 3.13})'
labels: 'port, scaffolding, ci'
---

## Source (TypeScript)
- File: `N/A — new file`
- Lines: —
- Function(s)/class(es): —

The TS source's CI config is not a reference — LCM ships under OpenClaw's GH Actions setup, which is Node/TypeScript-shaped (vitest + esbuild). Python CI is a clean greenfield setup driven by ADR-005 (Python version target), `docs/reference/dependencies.md` lines 142-160 (platform support matrix), and `docs/porting-guides/tests-and-config.md` lines 498-535 (recommended workflow YAML).

## Target (Python)
- File: `.github/workflows/ci.yml`, `.github/workflows/lint.yml` (optional split)
- Estimated LOC: ~80-120 LOC of GH Actions YAML

## Dependencies
- Depends on: #00-01 (needs `pyproject.toml` with `[dev]` extra to install `ruff`, `ty`, `pytest`)
- Blocks: #00-08 (README quickstart cites the CI status badge)

## Acceptance criteria
- [ ] `.github/workflows/ci.yml` declares the matrix `{ os: [ubuntu-latest, macos-latest], python: ["3.11", "3.12", "3.13"] }` — 6 cells.
- [ ] Each job runs in order: `checkout` → `setup-python` → `uv pip install -e ".[dev]"` (or `uv sync --locked` once the lock is committed) → `ruff check .` → `ruff format --check .` → `ty check` → `pytest -m 'not live' --cov=lossless_hermes --cov-report=xml`.
- [ ] `uv sync --locked` is the install step once `uv.lock` is checked in (supply-chain gate per ADR-006 §Consequences — "fails closed if a transitive shifted").
- [ ] CI fails closed when `uv sync --locked` detects a transitive shift.
- [ ] `pytest` invocation excludes `live` markers by default (`-m 'not live'`), matching the marker scheme in `dependencies.md` lines 107-111 and ADR-028 §Decision point 10.
- [ ] Codecov upload step on `ubuntu-latest, python-3.12` only (one upload per build is enough; matrix-wide upload double-counts).
- [ ] Separate workflow job `live-voyage` is defined but gated on `if: github.event_name == 'push' && github.ref == 'refs/heads/main'` — does NOT run on PR jobs (per `tests-and-config.md` line 519-530). Uses `${{ secrets.VOYAGE_API_KEY }}`.
- [ ] A no-op smoke step verifies the entry point loads: `python -c "from importlib.metadata import entry_points; assert any(ep.name == 'lossless-hermes' for ep in entry_points(group='hermes_agent.plugins'))"`.
- [ ] CI uses `actions/setup-python@v5` and `actions/checkout@v4` (or whichever majors are current).
- [ ] Caching: `actions/setup-python` `cache: 'pip'` (or `uv`'s native cache) keyed on `uv.lock` hash — keeps first-pull times short.
- [ ] All 6 cells go green on the PR that adds this workflow.
- [ ] A "CI" status badge in the top-level README references this workflow.

## Estimated effort
5 hours (most of it is debugging the macOS arm64 cell — `sqlite-vec` wheel + stdlib `sqlite3.enable_load_extension` quirks per spike 001 §Findings).

## Confidence
90% — the workflow shape is well-specified by `tests-and-config.md` lines 498-535, but until first-hand-run on all 6 cells, the "inferred" platform cells in `dependencies.md` lines 144-160 carry the 10% residual risk. Mitigation: this PR is itself the conversion of "inferred → tested" for every cell.

## Files to read before starting
- `docs/adr/005-python-version.md` (Python floor + recommended CI matrix per §Consequences)
- `docs/reference/dependencies.md` lines 142-160 (platform support matrix; "Action item: First PR should add a GitHub Actions matrix...")
- `docs/porting-guides/tests-and-config.md` lines 498-535 (recommended CI workflow YAML — this issue is its Python-port equivalent)
- `docs/adr/006-dependency-pinning.md` §Consequences (`uv sync --locked` fail-closed gate)
- `docs/adr/028-vitest-to-pytest.md` §Decision point 10 (`live` marker semantics)
- `docs/spike-results/001-sqlite-vec-python.md` (platform-specific gotchas — Apple system Python is poisoned)
