---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-00] scaffolding: initialize pyproject.toml + src package skeleton'
labels: 'port, scaffolding'
---

## Source (TypeScript)
- File: `N/A — new file`
- Lines: —
- Function(s)/class(es): —

The TS source's `package.json` (`lossless-claw/package.json`) is not a useful reference here — npm/Node packaging differs structurally from PyPI/setuptools/hatchling. This issue is a clean greenfield setup driven by ADR-001, ADR-005, ADR-006, ADR-007, ADR-008, and the `pyproject.toml` skeleton in `docs/reference/dependencies.md` lines 49-140.

## Target (Python)
- File: `pyproject.toml`, `src/lossless_hermes/__init__.py`, `src/lossless_hermes/py.typed`, `.gitignore`, `LICENSE`
- Estimated LOC: ~140 LOC (`pyproject.toml`) + ~20 LOC (`__init__.py` stub) + boilerplate

## Dependencies
- Depends on: — (entry-point issue for the entire port)
- Blocks: every other issue in Epic 00, and transitively every later issue in the port

## Acceptance criteria
- [ ] `pyproject.toml` exists and matches the skeleton in `docs/reference/dependencies.md` lines 49-140 verbatim (with corrections per the ADRs cited below).
- [ ] `[build-system]` selects setuptools or hatchling; `[tool.hatch.build.targets.wheel] packages = ["src/lossless_hermes"]` if hatchling, per ADR-024 §Consequences.
- [ ] `[project]` declares `name = "lossless-hermes"`, `version = "0.1.0"`, `requires-python = ">=3.11"` (matches Hermes — ADR-005).
- [ ] `[project.dependencies]` lists exactly the 5 runtime pins from `dependencies.md` line 13-17 (`httpx[socks]==0.28.1`, `sqlite-vec==0.1.9`, `pydantic==2.12.5`, `pyyaml==6.0.3`, `tenacity==9.1.4`). No `hermes-agent` entry (ADR-007).
- [ ] Every direct dep is `==X.Y.Z` exact-pinned (ADR-006). CI lint regex catches any non-exact pin.
- [ ] `[project.optional-dependencies]` declares `apsw`, `dev`, and `type-mypy` extras per `dependencies.md` lines 79-94.
- [ ] `[project.entry-points."hermes_agent.plugins"]` declares `lossless-hermes = "lossless_hermes:register"` exactly as specified in ADR-001 §Decision.
- [ ] `src/lossless_hermes/__init__.py` exists and exports a no-op `def register(ctx): pass` callable (filled in by #00-06).
- [ ] `src/lossless_hermes/py.typed` exists (PEP 561 marker — enables downstream `ty` type-checking).
- [ ] `uv lock` succeeds and produces a `uv.lock` checked into the repo (ADR-006 §Consequences).
- [ ] `uv pip install -e .` succeeds in a fresh venv on macOS arm64 with Homebrew Python 3.12.
- [ ] `python -c "import lossless_hermes; lossless_hermes.register(None)"` runs without error (no-op).
- [ ] `python -c "from importlib.metadata import entry_points; print(entry_points(group='hermes_agent.plugins'))"` lists `lossless-hermes` after install.
- [ ] `.gitignore` covers `*.pyc`, `__pycache__/`, `.venv/`, `dist/`, `build/`, `*.egg-info/`, `.pytest_cache/`, `.ruff_cache/`, `.ty_cache/`, `.coverage`, `htmlcov/`.
- [ ] `LICENSE` exists with MIT license text (`[project] license = { text = "MIT" }` per `dependencies.md` line 62).

## Estimated effort
6 hours

## Confidence
95% — the skeleton is fully specified by `dependencies.md` and ADRs 001/005/006/007/024. Residual risk is debugging `uv lock` resolution on first run if a transitive pin conflicts; bumping a non-direct pin in `uv.lock` is the resolution.

## Files to read before starting
- `docs/reference/dependencies.md` (entire document — the `pyproject.toml` skeleton lives at lines 49-140)
- `docs/adr/001-plugin-distribution-model.md` (entry-point group + `register(ctx)` shape)
- `docs/adr/005-python-version.md` (`requires-python = ">=3.11"`)
- `docs/adr/006-dependency-pinning.md` (exact `==X.Y.Z` policy + `uv.lock` requirement)
- `docs/adr/007-hermes-as-dependency.md` (do NOT pin `hermes-agent`)
- `docs/adr/008-typechecker.md` (`ty==0.0.21` primary; `mypy==1.13.0` opt-in via `[type-mypy]`)
- `docs/adr/024-project-layout.md` (`src/lossless_hermes/` layout + `[tool.hatch.build.targets.wheel]` block)
