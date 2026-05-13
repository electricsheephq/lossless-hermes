# ADR-008: Typechecker

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

The lossless-hermes codebase is annotated end-to-end. CI must run a static type checker as a gate. Python's type-checker ecosystem (2026) offers four mature options:

1. **`mypy`** — the reference implementation. PEP-484 era. Slow, but most permissive about plugin ecosystems.
2. **`pyright`** — Microsoft's TypeScript-style checker. Fast, used as VS Code's Python language server. Stricter inference.
3. **`pyre`** — Meta's checker. Internal-dialect focus; less common in OSS.
4. **`ty`** — Astral's checker (sibling of `ruff`). Pre-1.0 (`0.0.21` at writing). Designed to be drop-in fast.

The choice affects: CI speed, error-message conventions, configuration syntax (`pyproject.toml` table), and whether contributors can run the same checker locally as CI.

## Options considered

### Option A: `ty==0.0.21` (primary) + `mypy==1.13.0` available via `[type-mypy]` extra

- Description: `ty` is the CI gate. `mypy` is offered as an opt-in extra for contributors whose IDE or workflow needs it.
- Pros:
  - **Matches Hermes exactly.** Hermes uses `ty` in CI; `dependencies.md` line 43-47 cites:
    > **Decision: ty over mypy/pyright.** Rationale: Hermes already runs `ty` in CI (see `[tool.ty.environment]` in Hermes `pyproject.toml` lines 244-251). Using the same type-checker means our error messages and rule semantics align with the host.
  - **Shared rule semantics with host.** Contributors who already work on Hermes don't need to learn a second checker's idiosyncrasies. False-positive triage is consistent across both codebases.
  - **Speed.** Astral's checkers (ruff, ty) are written in Rust; ty is materially faster than mypy on large codebases. CI feedback loop is shorter.
  - **Same tool family as `ruff`.** We already use `ruff==0.15.10` (matches Hermes pin — `dependencies.md` line 42). Adopting `ty` consolidates the toolchain under one vendor (Astral).
  - **`pyproject.toml` config alignment.** Hermes uses `[tool.ty.environment] python-version = "3.13"` (`dependencies.md` line 126-127). We mirror it verbatim.
  - **`mypy` fallback covers breakage windows.** `ty` is pre-1.0 (0.0.21). If a `ty` bump breaks our codebase, contributors can switch to `mypy` via the `[type-mypy]` extra and continue working while we fix the `ty` config (`dependencies.md` line 44 documents this).
- Cons:
  - **`ty` is pre-1.0 (0.0.21).** API may shift in load-bearing ways before 1.0. `dependencies.md` Open-decisions section lists this as a tracked risk: "`ty` is at `0.0.21` — pre-1.0, API may shift."
  - **Smaller community / plugin ecosystem.** mypy has a richer set of third-party plugins (e.g. `pydantic-mypy`, `django-stubs`); ty's plugin story is younger.
- Evidence cited:
  - Hermes precedent: `dependencies.md` line 43-47 (verbatim quote above).
  - `ty` pin: `dependencies.md` line 43 (`ty==0.0.21`).
  - `mypy` fallback rationale: `dependencies.md` line 44 ("`mypy` is offered as an opt-in fallback for environments where ty isn't available").
  - Config alignment: `dependencies.md` line 126-127 (`python-version = "3.13"`).
  - Pre-1.0 risk: `dependencies.md` Open decisions §"ADR-007 (pending): `ty` vs `mypy` vs both?".

### Option B: `mypy==1.13.0` as primary

- Description: standard mypy in CI; `ty` not used.
- Pros:
  - **Most mature.** PEP-484 reference implementation; battle-tested.
  - **Richer plugin ecosystem.** `pydantic-mypy`, `sqlmypy`, etc.
  - **Conservative choice.** No pre-1.0 risk.
- Cons:
  - **Diverges from Hermes.** Hermes uses `ty`; our errors and rule semantics would differ from the host's. Contributors who work on both must learn two configs.
  - **Slower CI feedback.** mypy on a multi-thousand-line codebase can take tens of seconds; ty is order-of-magnitude faster.
  - **Three different tools in toolchain.** ruff (Astral) + mypy (independent) + something else for formatting (ruff or black) is more inconsistent than ruff + ty.
- Evidence cited:
  - Hermes's choice and rationale: `dependencies.md` line 43-47.

### Option C: `pyright` as primary

- Description: Microsoft's pyright in CI; ty / mypy not used.
- Pros:
  - **Used by VS Code's Python extension.** Contributors who use VS Code already see pyright diagnostics inline.
  - **Fast (TypeScript-compiled).**
  - **Stricter inference than mypy.**
- Cons:
  - **Diverges from Hermes.** Hermes uses `ty`.
  - **Reads `pyproject.toml` config but with a different schema.** Adopting pyright means a third toolchain config in addition to ruff and (potentially) ty.
  - **Less common in OSS CI gates.** Adoption is heavier on the IDE side than CI side.
- Evidence cited:
  - `dependencies.md` line 47: "Pyright is omitted to avoid a third type-checker — if a contributor uses VS Code, the Pyright extension can still consume `ty`'s `pyproject.toml` config without us pinning Pyright as a dep." Pyright as an IDE plug-in continues to work without our explicit support.

### Option D: Both ty and mypy in CI (dual-gate)

- Description: every CI run executes both ty and mypy; any failure blocks merge.
- Pros: maximum coverage; catches checker-specific blind spots.
- Cons:
  - **CI cost doubles.** Two checker runs on every PR.
  - **Contradictory errors.** ty and mypy disagree on edge cases (`# type: ignore` syntax, narrowing rules). Resolving disagreement adds friction.
  - **Bumps need joint reconciliation.** A ty bump and a mypy bump in separate PRs may both pass independently but fail together.
- Evidence cited: `dependencies.md` Open decisions §"ADR-007 (pending)" leans against dual-gate for friction reasons.

## Decision

Chosen: **Option A — `ty==0.0.21` (primary CI gate) + `mypy==1.13.0` available via `[type-mypy]` extra.**

## Rationale

Quoting `dependencies.md` line 43-47:

> **Decision: ty over mypy/pyright.** Rationale: Hermes already runs `ty` in CI (see `[tool.ty.environment]` in Hermes `pyproject.toml` lines 244-251). Using the same type-checker means our error messages and rule semantics align with the host. `mypy` is offered as an opt-in fallback for environments where ty isn't available. Pyright is omitted to avoid a third type-checker — if a contributor uses VS Code, the Pyright extension can still consume `ty`'s `pyproject.toml` config without us pinning Pyright as a dep.

The decision is dominated by host coherence. Hermes is in the same git tree (conceptually) — contributors working on both packages benefit when the checker behavior is identical. Sharing `ty` means:

- Same `# type: ignore` syntax expectations across both codebases.
- Same narrowing/inference rules — code that ty-checks clean in Hermes will ty-check clean here, and vice versa.
- Same CI feedback loop length (both fast).

`mypy` as opt-in covers two real cases:

1. Contributors using IDE plugins that don't yet speak `ty` (most IDE Python integrations as of 2026 still rely on mypy or pyright).
2. The `ty` bump breakage window — if a `ty==0.0.22` bump introduces a regression, contributors can run mypy locally while we fix the ty config.

Option C (pyright) was rejected explicitly per `dependencies.md` line 47 — pyright works as an IDE extension without us pinning it, so we get IDE coverage for free without committing to a third checker in CI.

Option D (dual-gate) was rejected because the friction of resolving inter-checker disagreement exceeds the value of redundant coverage.

## Consequences

- **`pyproject.toml` declares `ty==0.0.21` in `[dev]`.** Per `dependencies.md` line 90 (`ty==0.0.21,  # matches Hermes pin`).
- **`pyproject.toml` declares `mypy==1.13.0` in `[type-mypy]` opt-in extra.** Per `dependencies.md` line 94 (`type-mypy = ["mypy==1.13.0"]`).
- **`[tool.ty.environment]` configures `python-version = "3.13"`.** Matches Hermes's pin; the target checker version is decoupled from the runtime floor (`requires-python = ">=3.11"` per ADR-005). Per `dependencies.md` line 126-127.
- **`[tool.ty.rules]` carries our default rule overrides.** Starting point per `dependencies.md` line 129-130: `unknown-argument = "warn"`, `redundant-cast = "ignore"`. Tightened over time.
- **CI runs `ty check` as a required gate.** Failure blocks merge.
- **`pre-commit` runs `ty check` as a fast local gate.** Contributors get feedback before pushing (per `dependencies.md` line 45 — `pre-commit==4.0.1` runs `ty check` per the documented hook config).
- **Pyright IDE users are first-class.** VS Code Pyright extension reads our `pyproject.toml` config without explicit support from us. Documented in CONTRIBUTING.
- **mypy fallback workflow is documented.** README / CONTRIBUTING covers: `pip install lossless-hermes[type-mypy]; mypy lossless_hermes/`. Useful for IDE integrations and as a `ty` breakage circuit-breaker.
- **`ty` bumps are deliberate.** Per `dependencies.md` Open-decisions: "Mitigation: pin exactly; bump deliberately; the `mypy` fallback covers any breakage window."
- **Precluded:** dual-gate ty+mypy in CI (Option D rejected for friction). Pyright as a pinned CI dep (Option C rejected).
- **Invariant:** every commit on main passes `ty check` with zero errors and zero warnings (the `[tool.ty.rules]` table controls which rules are warnings vs errors).

## Open questions / 5% uncertainty

1. **`ty` 1.0 timing.** `ty` is at 0.0.21; the path to 1.0 is unclear. If `ty` releases a 0.1 or 1.0 with breaking config changes, we may need a multi-PR migration. Mitigation: pin exactly; bump deliberately; `mypy` fallback covers any breakage window (per `dependencies.md` Open-decisions).
2. **Pyright IDE coverage drift.** Pyright consumes `pyproject.toml` config but has its own rule semantics. Contributors may see different errors in VS Code than CI reports. Mitigation: document that CI is authoritative; pyright IDE diagnostics are advisory.
3. **mypy plugin compatibility.** If we adopt `pydantic-mypy` for richer Pydantic introspection under the `[type-mypy]` extra, we'd need to verify it still works on `mypy==1.13.0`. Defer until concrete need.
4. **`ty` plugin ecosystem maturity.** ty's plugin story (e.g. for Pydantic, SQLAlchemy) is younger than mypy's. If a critical false-positive arises that mypy resolves cleanly via a plugin and ty doesn't, we have an escape hatch (toggle CI to mypy temporarily) but it's friction. Documented; no action for v0.1.
5. **Cross-platform `ty` wheels.** `ty==0.0.21` should ship Rust-compiled wheels for our platform matrix (macOS arm64/x86_64, Linux x86_64/arm64). Not exercised first-hand on every platform; expected to work given Astral's reach. CI matrix in first PR closes this.