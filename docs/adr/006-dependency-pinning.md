# ADR-006: Dependency pinning policy

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

`pyproject.toml`'s `dependencies` list controls what `pip install lossless-hermes` resolves. There are three commonly-used pinning strategies:

1. **Unpinned / version ranges** (`httpx`, `httpx>=0.27`, `httpx~=0.28`) — the resolver picks the latest compatible version.
2. **Compatible-release pins** (`httpx==0.28.*`, `httpx~=0.28.0`) — fixes major/minor; floats patch.
3. **Exact pins** (`httpx==0.28.1`) — fixes every component; transitive deps still float until `uv.lock` is checked in.

Each strategy trades flexibility against reproducibility. The choice is load-bearing for a plugin that runs inside another tool's process (Hermes), where a transitive that arrives during an upgrade can break the host's resolver state.

## Options considered

### Option A: Exact `==X.Y.Z` pins on every direct runtime dependency

- Description: every entry in `dependencies` and `optional-dependencies` is `==X.Y.Z`. `uv.lock` checked into the repo. Bumps are intentional and reviewed.
- Pros:
  - **Mirrors Hermes's policy verbatim.** Hermes's `pyproject.toml` lines 14-39 use exact pins on every direct runtime dep. `dependencies.md` line 4-5 documents the rationale: "we mirror Hermes's policy verbatim".
  - **Mini Shai-Hulud worm (2026-05-12) — the canonical case study.** `dependencies.md` line 5: "the 2026-05-12 Mini Shai-Hulud worm hitting `mistralai 2.4.6` is the canonical case study." Quoting the full context from `dependencies.md` line 4-5 verbatim:

    > **Pinning policy:** **Exact pins** (`==X.Y.Z`) on every direct runtime dependency. We mirror Hermes's policy verbatim — see Hermes `pyproject.toml` lines 14-39 for the rationale, but in short: ranges let PyPI ship a fresh transitive into our installs without a review on our side, and the 2026-05-12 Mini Shai-Hulud worm hitting `mistralai 2.4.6` is the canonical case study. Bump pins intentionally; regenerate `uv.lock` on every bump.

    A range like `mistralai>=2.4.0` would have automatically pulled the worm-infected 2.4.6 into every fresh install during the active window.
  - **Reproducible installs.** Any two `pip install lossless-hermes` runs at the same commit produce identical dependency graphs (assuming `uv.lock` is committed).
  - **CI can fail closed.** `uv sync --locked` fails if a transitive shifted since lock generation. `dependencies.md` Remaining-risk row "Supply-chain attack on a transitive dep" credits this as the mitigation.
  - **Pin parity with Hermes.** `httpx[socks]==0.28.1`, `pydantic==2.12.5`, `pyyaml==6.0.3`, `tenacity==9.1.4` all match Hermes's pins exactly (`dependencies.md` lines 13-17). Operators installing both packages into one env never see a resolver conflict.
- Cons:
  - **Bumps require manual review.** Every dep upgrade is a small PR (review the changelog, regenerate `uv.lock`, run tests). Cannot just `pip install -U`.
  - **Pre-1.0 deps need patch-level bumps too.** `sqlite-vec==0.1.9` cannot float to `0.1.10` without a deliberate bump. This is the right policy (pre-1.0 APIs do break) but it's overhead.
- Evidence cited:
  - Hermes precedent: `/Volumes/LEXAR/Claude/hermes-agent/pyproject.toml` lines 14-39.
  - `dependencies.md` line 4-5 (verbatim quote above).
  - Mini Shai-Hulud worm: `dependencies.md` line 5; `mistralai 2.4.6` worm date 2026-05-12.
  - `dependencies.md` Remaining-risk table row: "Supply-chain attack on a transitive dep (Mini Shai-Hulud style) ... Mitigation: Exact pins on direct deps + `uv.lock` checked into the repo. `uv sync --locked` in CI fails closed if a transitive shifts."

### Option B: Compatible-release pins (`~=X.Y.0` or `>=X.Y,<X.(Y+1)`)

- Description: pin major/minor; allow patch updates.
- Pros:
  - Auto-pulls bugfixes without manual review.
  - Slightly easier dependency hygiene.
- Cons:
  - **Doesn't protect against supply-chain compromise of a patch.** `mistralai 2.4.6` was a patch release; `mistralai~=2.4.0` would have pulled it.
  - **Diverges from Hermes.** Hermes uses exact pins; we'd be looser, which means the resolver may pick different versions when installing both. Defeats the "pin parity" benefit.
  - **Patch-level breakage in pre-1.0 deps.** `sqlite-vec==0.1.*` would allow 0.1.10 with whatever API shift it brings — exactly the wrong behavior for pre-1.0.
- Evidence cited:
  - Same Mini Shai-Hulud case: a patch-release worm in a transitive bypasses minor-locked ranges. `dependencies.md` line 5.

### Option C: Unpinned / `>=` only

- Description: declare `httpx`, `sqlite-vec`, etc. without version constraints, or with loose `>=` floors.
- Pros:
  - Maximum flexibility for the user's resolver.
  - No bump friction.
- Cons:
  - **Supply chain wide open.** Any PyPI compromise lands automatically.
  - **No reproducibility.** Two installs a week apart produce different graphs.
  - **Hermes resolution conflicts.** Hermes pins exact versions; if we pin nothing, the resolver may pick versions that conflict with Hermes's pins (or pick the "newest compatible," which may be a regression).
  - **CI cannot fail closed.** With unpinned deps and no lock, `uv sync` can't detect transitive shifts.
- Evidence cited:
  - Inverse of every Option A pro.

## Decision

Chosen: **Option A — exact `==X.Y.Z` pins on every direct runtime dependency. `uv.lock` checked into the repo. Bumps regenerate the lock and run the full test suite.**

## Rationale

Quoting `dependencies.md` line 4-5 verbatim — the rationale stands as written:

> **Pinning policy:** **Exact pins** (`==X.Y.Z`) on every direct runtime dependency. We mirror Hermes's policy verbatim — see Hermes `pyproject.toml` lines 14-39 for the rationale, but in short: ranges let PyPI ship a fresh transitive into our installs without a review on our side, and the 2026-05-12 Mini Shai-Hulud worm hitting `mistralai 2.4.6` is the canonical case study. Bump pins intentionally; regenerate `uv.lock` on every bump.

Three forces converge on Option A:

1. **Supply-chain risk.** The Mini Shai-Hulud worm (2026-05-12, `mistralai 2.4.6`) is the canonical case — a patch-level compromise in a popular dep. Exact pins + locked transitives + `uv sync --locked` in CI is the minimum defense.
2. **Host-coherence.** Hermes pins exact versions on shared deps (`httpx`, `pydantic`, `pyyaml`, `tenacity`, `pytest`, `pytest-asyncio`, `ruff`, `ty`). Matching its pins eliminates resolver thrash when both packages install into the same env (`dependencies.md` lines 13-17 cite "matches Hermes pin" on every shared dep).
3. **Pre-1.0 API churn.** `sqlite-vec==0.1.9` is pre-1.0; even patch bumps can break (`dependencies.md` Remaining-risk row "sqlite-vec is still 0.1.x"). Range pins would silently break us.

Option B (compatible-release) was rejected because the canonical attack we're defending against (`mistralai 2.4.6`) bypasses minor-locked ranges by being a patch release. Option C (unpinned) was rejected because it gives up reproducibility and host-coherence with no countervailing benefit.

## Consequences

- **Every direct dep in `pyproject.toml` `dependencies` and `optional-dependencies` is `==X.Y.Z`.** Current v0.1 set, from `dependencies.md` lines 13-17, 29, 38-45:
  - Runtime: `httpx[socks]==0.28.1`, `sqlite-vec==0.1.9`, `pydantic==2.12.5`, `pyyaml==6.0.3`, `tenacity==9.1.4`.
  - `[apsw]` extra: `apsw==3.53.1.0`.
  - `[dev]` extra: `pytest==9.0.3`, `pytest-asyncio==1.3.0`, `pytest-mock==3.14.0`, `pytest-cov==6.0.0`, `respx==0.22.0`, `ruff==0.15.10`, `ty==0.0.21`, `pre-commit==4.0.1`.
  - `[type-mypy]` extra: `mypy==1.13.0`.
- **`uv.lock` is checked into the repo.** Generated by `uv lock`; regenerated on every dep bump.
- **CI gate: `uv sync --locked`.** Fails if a transitive shifted since lock generation. Documented in `dependencies.md` Remaining-risk-table mitigation column for the supply-chain row.
- **Bump workflow is a PR.** Each dep bump = one PR, with the changelog quoted in the PR description, full test suite green, and `uv.lock` regenerated.
- **`hermes-agent` is NOT pinned.** Per ADR-007 (next), Hermes is host-installed separately. Adding it to `dependencies` would make `uv lock` fail because Hermes is not on PyPI as of 2026-05-13.
- **Shared deps mirror Hermes exactly.** `httpx==0.28.1`, `pydantic==2.12.5`, `pyyaml==6.0.3`, `tenacity==9.1.4`, `pytest-asyncio==1.3.0`, `ruff==0.15.10`, `ty==0.0.21` all match Hermes's pins per `dependencies.md`. When Hermes bumps, we follow within one PR. **Exception:** `pytest==9.0.3` is pinned **one patch above** Hermes's pin — it was bumped to close a moderate GHSA tmpdir CVE (dependabot #1); see the inline comment in `pyproject.toml`. The exact-pin policy is unchanged; the value diverges from Hermes by one patch for a security fix.
- **Precluded:** ranges, compatible-release operators (`~=`), and unbounded `>=`. The only floor-only declaration in our `pyproject.toml` is `requires-python = ">=3.11"` (the Python interpreter itself; not a PyPI dep).
- **Invariant:** every direct PyPI dep is `==X.Y.Z`. CI lints the `pyproject.toml` for any non-exact pin (a one-line regex check is sufficient).

## Open questions / 5% uncertainty

1. **Bump cadence vs upstream security advisories.** When a CVE drops, we need to bump fast. Process: a security-advisory monitor (GitHub Dependabot or pip-audit on a schedule) flags affected versions; the bump is a fast-track PR. Document the SLA in CONTRIBUTING (e.g. "high-severity CVE = same-day bump if a fix is available").
2. **Transitive pins via `uv.lock`.** Direct pins protect us from direct-dep compromise; the lock file protects against transitive shifts. The lock is only as good as its last regeneration. Mitigation: weekly `uv lock --upgrade` PR (auto-generated), review the diff, merge if tests pass.
3. **Pre-1.0 deps may break on minor bumps.** `sqlite-vec` (0.1.x) and `ty` (0.0.x) are both pre-1.0. Mitigation: documented in `dependencies.md` Remaining-risk table; budget one upgrade cycle per minor bump and run the full test suite.
4. **License audit on bump.** Each bump must re-validate the license (the package may have changed). Mitigation: `pip-licenses` in CI; fail if a dep's license becomes non-permissive without explicit override.
