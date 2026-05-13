---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-00] scaffolding: top-level README + CONTRIBUTING with install + quickstart'
labels: 'port, scaffolding, docs'
---

## Source (TypeScript)
- File: `lossless-claw/README.md` (the OpenClaw-side README — useful reference for the "what does LCM do?" prose, but most of its install instructions are OpenClaw-specific and do not port)
- Lines: N/A — Python README is a clean rewrite
- Function(s)/class(es): —

The current `README.md` (top-level, the Phase-1 architecture readme) is read-only context — this issue **replaces** it with a v0.1-ready user-facing README. The Phase-1 content survives but moves to `docs/architecture-overview.md` (or similar) so the top-level README is install-quickstart-first.

## Target (Python)
- File: `README.md` (rewrite), `CONTRIBUTING.md` (new), optionally `docs/architecture-overview.md` (move-target for the existing Phase-1 prose)
- Estimated LOC: ~150-250 LOC of Markdown (README) + ~100-150 LOC (CONTRIBUTING)

## Dependencies
- Depends on:
  - #00-01 (`pyproject.toml` exists — install instructions reference it)
  - #00-02 (CI badge in README references the workflow)
  - #00-03 (CONTRIBUTING instructs `pre-commit install`)
  - #00-04 (CONTRIBUTING instructs `pytest -m 'not live'`)
  - #00-05, #00-06, #00-07 (the README's "what's in v0.1" section enumerates what works)
- Blocks: — (this is the last issue in Epic 00)

## Acceptance criteria
- [ ] `README.md` opens with a 1-paragraph TL;DR: "Lossless Context Management plugin for Hermes-agent. Ported from `Martian-Engineering/lossless-claw` (TypeScript/OpenClaw). v0.1 is a green-CI scaffolding release — the engine is a no-op passthrough; real LCM behavior lands incrementally across Epics 01-09."
- [ ] **Status banner.** Top-of-README badge area:
  - [ ] CI status badge (links to `.github/workflows/ci.yml`)
  - [ ] Python version support badge (`Python 3.11 | 3.12 | 3.13`)
  - [ ] License badge (MIT)
- [ ] **Install section** with two steps in exact order:
  1. Install Hermes (curl-bash or `uv pip install -e` from source — per ADR-007 §Consequences "Documentation lift in README"). Quote the curl-bash one-liner from the Hermes README.
  2. `uv pip install lossless-hermes` into the same Python env. Show both `uv pip` and `pip` variants.
- [ ] **Quickstart section** with the minimal `~/.hermes/config.yaml`:

      ```yaml
      context:
        engine: lcm

      plugins:
        enabled:
          - lossless-hermes
      ```

  And the one-liner to verify: `hermes` (session starts; LCM is selected; engine is a no-op passthrough). Cite ADR-001 §Consequences for the "must add to `plugins.enabled`" and "must set `context.engine: lcm`" requirements.
- [ ] **Platform support matrix** — copy/adapt from `docs/reference/dependencies.md` lines 144-160, narrowing to v0.1's tested cells. Highlight that Apple `/usr/bin/python3` is unsupported (per ADR-004 §Consequences + ADR-005 §Consequences).
- [ ] **Recommended Python** = 3.12 (Homebrew, pyenv, or uv-managed), per ADR-005 §Decision rationale.
- [ ] **Naming convention split** documented per ADR-023 §Open questions: PyPI dist name is `lossless-hermes` (hyphenated); Python module is `lossless_hermes` (snake_case); YAML config namespace is `lossless_hermes:` (snake_case). Operators type the snake_case form in `config.yaml`; they only see the hyphenated form when running `pip install` or `pip uninstall`.
- [ ] **What v0.1 does NOT do** — explicit list of every Epic-01-through-09 feature, marked "coming in Epic 0N". This sets expectations and avoids "I installed lossless-hermes and it has no /lcm command???" reports.
- [ ] **Pointers** section linking to:
  - [ ] `docs/` (ADRs, porting guides, spike results)
  - [ ] `epics/` (10-epic breakdown + per-issue specs)
  - [ ] `CONTRIBUTING.md`
  - [ ] Upstream: Hermes-agent repo, lossless-claw source repo, PR #613 (omnibus), PR #628 (stub-tier)
- [ ] `CONTRIBUTING.md` exists with:
  - [ ] Setup: clone, `uv venv`, `uv pip install -e ".[dev]"`, `pre-commit install`
  - [ ] Test policy: `pytest -m 'not live'` is the default; `live` markers require API keys; CI runs them on `main` only
  - [ ] Type check: `ty check` is the gate; `[type-mypy]` extra exists as a fallback (ADR-008)
  - [ ] Lint + format: `ruff check` + `ruff format`
  - [ ] Commit hooks: `pre-commit install` enforces the above; `--no-verify` is discouraged
  - [ ] Branch policy + PR review expectations (cite the `.github/ISSUE_TEMPLATE/port-issue.md` template)
  - [ ] Dependency bump workflow (per ADR-006 §Consequences "Bump workflow is a PR")
  - [ ] Apple `/usr/bin/python3` UNSUPPORTED warning with one-line fix instructions
- [ ] The existing Phase-1 architecture README content (current `README.md`) is preserved under `docs/architecture-overview.md` (or its `Source of truth` table is moved into `docs/README.md`) so no information is lost.
- [ ] Markdown renders cleanly on GitHub (no broken links, no missing alt text on badges).
- [ ] All file paths cited in README/CONTRIBUTING resolve to real files in the tree at PR-open time.

## Estimated effort
4 hours

## Confidence
95% — README content is well-specified by the cited ADRs and the existing top-level README's structure. Residual risk is minor copy-edit cycles to keep tone consistent with Hermes's README.

## Files to read before starting
- `README.md` (current Phase-1 README — preserve its links + intent)
- `docs/adr/001-plugin-distribution-model.md` §Consequences (install + config steps)
- `docs/adr/004-sqlite3-backend.md` §Consequences (Apple system Python guard)
- `docs/adr/005-python-version.md` §Consequences (3.11+ floor; 3.12 recommended)
- `docs/adr/006-dependency-pinning.md` §Consequences (bump workflow)
- `docs/adr/007-hermes-as-dependency.md` §Consequences (install ordering — Hermes first, then plugin)
- `docs/adr/008-typechecker.md` §Consequences (CONTRIBUTING type-check instructions)
- `docs/adr/023-config-delivery.md` §Open questions (naming-convention split visibility)
- `docs/reference/dependencies.md` (platform support matrix at lines 144-160)
- `docs/reference/hermes-hooks.md` lines 306-318 (`config.yaml` worked example)
- Upstream reference: `/Volumes/LEXAR/Claude/hermes-agent/README.md` (tone + structure to align with)
