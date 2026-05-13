# ADR-007: Hermes-agent as dependency

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

Lossless-hermes is a Hermes plugin. At runtime it imports from Hermes's namespace (`from agent.context_engine import ContextEngine`, `from hermes_cli.config import load_config`, `from hermes_constants import get_hermes_home`, etc. — see the worked example at `hermes-hooks.md:256-284`). The plugin cannot function without Hermes being importable.

The question: should `hermes-agent` appear in `pyproject.toml`'s `dependencies` (so that `pip install lossless-hermes` automatically pulls it in)?

The answer is constrained by two hard facts:

1. **`hermes-agent` is not on PyPI** as of 2026-05-13. Hermes is distributed via:
   - `curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash` (per Hermes README).
   - `uv pip install -e ".[all,dev]"` from a cloned source tree.
   - No PyPI release exists at `pypi.org/project/hermes-agent/`.
2. **Plugin discovery is by entry-point group**, not by import resolution. Hermes scans `importlib.metadata.entry_points(group="hermes_agent.plugins")` at startup (`/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:170,1039-1063`). The plugin's entry point is found regardless of how Hermes itself was installed.

## Options considered

### Option A: Do NOT pin hermes-agent; host-install separately

- Description: `pyproject.toml` does not list `hermes-agent` in `dependencies`. The README instructs operators to install Hermes first (via curl-bash or git clone + uv) and then `pip install lossless-hermes` into the same Python environment. Plugin discovery happens at Hermes startup via the entry-point group.
- Pros:
  - **It works today.** `uv lock` succeeds, `pip install lossless-hermes` succeeds, and the entry-point binding is found by Hermes at startup — regardless of whether the operator curl-installed Hermes or cloned it. The mechanism is well-trodden (`hermes_cli/plugins.py:1039-1063`).
  - **Honest about reality.** Hermes is not on PyPI, so we cannot pin it. Listing `hermes-agent==0.13.0` in `dependencies` would make `pip install lossless-hermes` fail with `No matching distribution found for hermes-agent` — every install would break.
  - **Plugin contract is canonical.** The entry-point group `hermes_agent.plugins` is the documented Hermes integration surface (`hermes-hooks.md` line 241-284, `dependencies.md` line 96-102). It works regardless of Hermes install method.
  - **`uv lock` succeeds.** `dependencies.md` lines 21-23 explicitly note: "adding `hermes-agent` as a `dependencies` entry would be a lie — pip cannot fetch it, and `uv lock` would fail."
  - **Operators expect host-managed plugins.** The Hermes plugin ecosystem (hindsight, honcho, langfuse, etc.) follows the same model — install Hermes, then install plugins into the same env. Operators have a mental model already.
- Cons:
  - **Silent failure if Hermes is missing.** `pip install lossless-hermes` succeeds in a Hermes-less environment, then fails at runtime when the entry point can't import `agent.context_engine`. Operators may not notice until they run Hermes and find no LCM tools. Mitigation: a startup health-check that emits an actionable error if Hermes is not on the path (covered in Consequences).
  - **No version coupling.** If Hermes upstream renames `agent.context_engine` to `hermes.context_engine`, lossless-hermes breaks at runtime with `ImportError`. We have no PyPI-resolver-level signal to refuse the install.
- Evidence cited:
  - PyPI status verified 2026-05-13: `dependencies.md` line 21-23 ("Hermes is **not on PyPI** as of 2026-05-13").
  - Entry-point group canonical name: `/Volumes/LEXAR/Claude/hermes-agent/hermes_cli/plugins.py:170`.
  - Entry-point load mechanism: `hermes_cli/plugins.py:1039-1063`.
  - `uv lock` failure mode: `dependencies.md` line 21-23.
  - Plugin contract documentation: `hermes-hooks.md` line 241-284 (recommended `pyproject.toml` shape).

### Option B: Pin `hermes-agent==X.Y.Z` (as soon as it ships to PyPI)

- Description: declare `hermes-agent==0.13.0` (or whatever version) in `dependencies`.
- Pros:
  - Resolver-level coupling: `pip install lossless-hermes` would also install Hermes.
  - Version compatibility is enforced (a Hermes that's too new/old won't resolve).
- Cons:
  - **Hermes is not on PyPI today.** This option is non-viable until Hermes publishes. As soon as a `hermes-agent` package appears on PyPI, this becomes feasible.
  - **Even if Hermes were on PyPI, exact pinning is too tight.** Hermes is a major moving target; pinning exact would force lossless-hermes bumps for every Hermes release. A range (`>=0.13,<0.14`) is more appropriate — minor versions of Hermes are unlikely to break the ContextEngine ABC (which is what we depend on).
  - **Curl-installed and source-installed Hermes may not register as importable from the resolver's perspective.** Even if Hermes is on PyPI, an operator who curl-installed Hermes from main and then `pip install lossless-hermes` may end up with two installed copies of `hermes-agent` (the curl one in `~/.hermes/...` PYTHONPATH, plus a PyPI release pulled in by our dep). Resolver can't see this conflict.
- Evidence cited:
  - PyPI absence: `dependencies.md` line 21-23.
  - Hermes ABC stability: `hermes-hooks.md` §"Remaining 5% risk" — `VALID_HOOKS` is hard-coded; ABC is stable across minor versions historically.

### Option C: Pin `hermes-agent` as a `[project.optional-dependencies]` extra (`[hermes]`)

- Description: pin to PyPI if available via opt-in (`pip install lossless-hermes[hermes]`). Default install does not pull Hermes.
- Pros: opt-in coupling for users who want PyPI-managed Hermes; default unaffected.
- Cons: same Hermes-not-on-PyPI problem — the extra fails to resolve today. Adds an install variant for an unclear win.
- Evidence: same as Option B.

## Decision

Chosen: **Option A — do NOT pin `hermes-agent`. Hermes is host-installed separately (curl-bash, or `uv pip install -e` from source). Plugin discovery is via the canonical `hermes_agent.plugins` entry-point group.**

## Rationale

The decision is forced by reality: Hermes is not on PyPI as of 2026-05-13 (verified — `dependencies.md` line 21-23). Listing it in `dependencies` would make every `pip install lossless-hermes` fail with `No matching distribution found for hermes-agent`, and `uv lock` would refuse to generate a lockfile.

The entry-point discovery mechanism is the explicit Hermes plugin contract (`hermes_cli/plugins.py:170`). Pip-installed plugins work via this contract regardless of how Hermes itself was installed. The hindsight, honcho, and langfuse plugins shipped under `/Volumes/LEXAR/Claude/hermes-agent/plugins/` follow the same model.

The silent-failure risk (operator installs lossless-hermes in a Hermes-less env, gets no error until startup) is real but mitigatable: a startup health-check in `lossless_hermes/__init__.py` that imports `agent.context_engine` and emits an actionable error closes the gap (mitigation called out in `dependencies.md` line 181).

Option B becomes viable the moment Hermes ships to PyPI. Adding `hermes-agent>=X.Y,<X.(Y+1)` (range, not exact pin — Hermes ABC is stable across minors; exact would force a lossless-hermes bump for every Hermes patch) would be the natural follow-up. Tracked in Open questions.

## Consequences

- **`pyproject.toml` `dependencies` list does NOT contain `hermes-agent`.** Operators install Hermes via the documented curl-bash or `uv pip install -e .` from source, then `pip install lossless-hermes` into the same Python environment.
- **Entry-point binding is the integration surface.** `[project.entry-points."hermes_agent.plugins"]` with `lossless-hermes = "lossless_hermes:register"` is the canonical declaration (per ADR-001).
- **Startup health-check required.** `lossless_hermes/__init__.py` imports `agent.context_engine` and `hermes_cli.config` at module load. If either import fails, the plugin's `register()` is wrapped in a try/except that emits a structured error: "lossless-hermes installed in an environment without hermes-agent on the path; install Hermes first."
- **Documentation lift in README.** README quickstart MUST cover both Hermes install (curl-bash or source) AND the `pip install lossless-hermes` step. Order matters — Hermes first.
- **CI must install Hermes for integration tests.** `tests/integration/*.py` requires a working Hermes import path. CI lane: `uv pip install -e <hermes-source-tree>` before `pytest`.
- **No version coupling at the resolver level.** A Hermes upstream rename of `hermes_agent.plugins` to `hermes.plugins` would break us at runtime with `ImportError`. Mitigation: pin a specific Hermes commit in our integration-test workflow; subscribe to Hermes release notes.
- **Precluded:** declaring `hermes-agent` anywhere in `pyproject.toml`'s `dependencies` or `optional-dependencies` until Hermes ships to PyPI.
- **Invariant:** the plugin is installable into any Python env via `pip install lossless-hermes` regardless of whether Hermes is present. Runtime errors are explicit and actionable, not silent ImportErrors.

## Open questions / 5% uncertainty

1. **When (if ever) does Hermes ship to PyPI?** Once Hermes publishes `hermes-agent` to PyPI, revisit this ADR. The natural successor decision is:
   - Add `hermes-agent>=X.Y,<X.(Y+1)` to `dependencies` (range, not exact pin — Hermes ABC is stable across minors; exact would force lossless-hermes bumps for every Hermes patch).
   - Keep the startup health-check (it covers the case where an operator force-installs the plugin without Hermes via `--no-deps`).
   - Update `dependencies.md` line 21-23 to reflect the new state.

   Tracked as a future ADR (provisionally ADR-009: "Hermes-agent dependency declaration once PyPI ships").

2. **Pinning Hermes git commit in tests.** Integration tests need a specific Hermes version. Options: (a) pin a git SHA in `tests/conftest.py` install step, (b) use a Hermes submodule, (c) install latest main and accept some test flake. Currently (a) — pin a SHA, bump deliberately. Document in CONTRIBUTING.

3. **Curl-installed Hermes path resolution.** Hermes installed via curl-bash places its source under `~/.hermes/...` and modifies the user's `PATH` and `PYTHONPATH`. Our `pip install lossless-hermes` into the same env relies on that path setup. If the env is wonky (e.g. activated virtualenv that doesn't inherit Hermes's path), lossless-hermes can install successfully but fail to find Hermes at runtime. Mitigation: startup health-check + documented "use the same Python that runs Hermes" guidance.

4. **Multiple Hermes versions on one host.** Power users may run multiple Hermes profiles backed by different Hermes versions. lossless-hermes installed once into one venv won't see the other Hermes. This is a non-goal for v0.1 — document "one lossless-hermes install per Hermes Python env."

5. **Hermes plugin discovery silently skipping on import error.** If `lossless_hermes:register` fails import (e.g. missing transitive), Hermes logs and continues without LCM (`hermes_cli/plugins.py:1218-1232`). The operator may not notice. Mitigation: structured logging from our health-check + `hermes lcm doctor status` CLI subcommand that prints the plugin's registration status.
