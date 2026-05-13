---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-00] scaffolding: pre-commit hooks (ruff, ty, end-of-file-fixer)'
labels: 'port, scaffolding, dx'
---

## Source (TypeScript)
- File: `N/A — new file`
- Lines: —
- Function(s)/class(es): —

LCM/OpenClaw uses Husky + lint-staged for git hooks (a Node/JS toolchain). The Python equivalent is `pre-commit` (the framework), which Hermes also uses. This issue is a clean greenfield setup driven by `dependencies.md` line 45 and ADR-008 §Consequences ("pre-commit runs `ty check` as a fast local gate").

## Target (Python)
- File: `.pre-commit-config.yaml`
- Estimated LOC: ~50-70 LOC of YAML

## Dependencies
- Depends on: #00-01 (needs `[dev]` extra to install `pre-commit==4.0.1`)
- Blocks: —

## Acceptance criteria
- [ ] `.pre-commit-config.yaml` exists with hooks (in order):
  - [ ] `trailing-whitespace` (from `pre-commit/pre-commit-hooks`)
  - [ ] `end-of-file-fixer` (from `pre-commit/pre-commit-hooks`)
  - [ ] `check-yaml` (from `pre-commit/pre-commit-hooks`) — validates `pyproject.toml` and workflow YAML are syntactically clean
  - [ ] `check-added-large-files` (from `pre-commit/pre-commit-hooks`, `args: ['--maxkb=500']`) — guards against accidentally committing a large fixture
  - [ ] `check-merge-conflict` (from `pre-commit/pre-commit-hooks`)
  - [ ] `mixed-line-ending` (from `pre-commit/pre-commit-hooks`, `args: ['--fix=lf']`)
  - [ ] `ruff check --fix` (from `astral-sh/ruff-pre-commit`, pinned `rev: v0.15.10` per `dependencies.md` line 42)
  - [ ] `ruff format` (from `astral-sh/ruff-pre-commit`, same `rev`)
  - [ ] `ty check` (local hook via `astral-sh/ty-pre-commit` if it exists, or `language: system` calling `ty check` from the active venv — `ty==0.0.21` per `dependencies.md` line 43)
- [ ] Hook revs are pinned to exact versions matching `dependencies.md`'s `[dev]` extras. No `rev: main` or floating refs.
- [ ] `pre-commit install` succeeds (writes `.git/hooks/pre-commit`).
- [ ] `pre-commit run --all-files` succeeds on a clean checkout.
- [ ] CONTRIBUTING.md (or a section in the README per #00-08) instructs new contributors to run `pre-commit install` after cloning.
- [ ] CI (#00-02) does NOT run pre-commit — CI's `ruff check` + `ty check` cover the same ground; pre-commit is a fast local gate only. This keeps CI from being a slower wrapper around the same checks.
- [ ] `--no-verify` workaround is documented as forbidden for normal commits — pre-commit hooks must be intentional escapes, not bypassed.

## Estimated effort
3 hours

## Confidence
95% — `pre-commit` is a well-understood tool; the only unknown is whether `ty` has a maintained `pre-commit` hook repo yet (it's pre-1.0). Fallback: `language: system` invoking the venv's `ty` binary. Tested before merge.

## Files to read before starting
- `docs/reference/dependencies.md` lines 45 (pre-commit==4.0.1 pin), 42-43 (ruff + ty pins)
- `docs/adr/008-typechecker.md` §Consequences ("pre-commit runs `ty check` as a fast local gate")
- `docs/adr/006-dependency-pinning.md` (exact-pin policy applies to pre-commit hook `rev:` too)
