# ADR-005: Python version target

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

`pyproject.toml` declares `requires-python = ">=X"`. The floor is load-bearing because:

- It controls which Python builds can install the package at all.
- It dictates which CPython-bundled SQLite versions we can assume (and therefore whether FTS5 trigram is available without a runtime probe).
- It bounds which language features (type hints, PEP-695 generics, `tomllib`, structural pattern matching) the implementation can use.
- It must be compatible with Hermes — operators install both Hermes and lossless-hermes into the same Python env (per ADR-001's entry-point distribution model).

## Options considered

### Option A: Python 3.11+ (3.12 sweet spot)

- Description: `requires-python = ">=3.11"`. Target Python 3.12 as the documented sweet spot.
- Pros:
  - **Matches Hermes exactly.** Hermes's own `pyproject.toml` declares `requires-python = ">=3.11"` (`/Volumes/LEXAR/Claude/hermes-agent/pyproject.toml` line 10, cited in `dependencies.md` line 3). Operators installing both packages into one env never see a version-resolver conflict.
  - **CPython 3.11 ships SQLite ≥ 3.39.** Spike 005 §Method step 8 documents the Python→SQLite version map: 3.11→3.39, 3.12→3.43, 3.13→3.45, 3.14→3.49+. The SQLite 3.34 floor for the trigram tokenizer (Dec 2020) is comfortably cleared by every supported Python.
  - **`sqlite-vec` wheel coverage.** Spike 001 §Method step 9 confirms PyPI wheels for Python 3.10/3.11/3.12/3.13/3.14 (pure-Python `py3-none-*` wheels, so the cpXY tag is irrelevant — same wheel for every minor).
  - **`apsw` wheel coverage.** Spike 001 confirms wheels for Python 3.10-3.13 across macOS arm64/x86_64 and Linux manylinux2014. (3.14 may need a wait if apsw lags.)
  - **Type-hint maturity.** Python 3.11 added the `Self` type, `TypeVarTuple`, `LiteralString`, `assert_never`, and `tomllib` (stdlib TOML reader). 3.12 added PEP-695 type-statement syntax. We can write idiomatic modern type-annotated code without `typing_extensions` for the common cases.
  - **Performance.** CPython 3.11 brought the specializing adaptive interpreter (~25% faster than 3.10 in mixed-workload benchmarks); 3.12 added more specializations and per-interpreter GIL. 3.12 is the production sweet spot per spike 001 §"Recommended Python stack".
  - **3.13 / 3.14 forward-compatibility.** All four currently-released minors (3.11, 3.12, 3.13, 3.14) pass spike 001 + spike 005 testing. No need to clamp an upper bound.
- Cons:
  - Operators on Python 3.10 (or below) cannot install. Python 3.10 reaches EOL in October 2026 per PEP 619; the cost is small and shrinking.
  - Apple's `/usr/bin/python3` (3.9.6) is automatically below the floor, which doubles as a feature given ADR-004's "system Python is unsupported" stance.
- Evidence cited:
  - Hermes floor: `/Volumes/LEXAR/Claude/hermes-agent/pyproject.toml` line 10; `dependencies.md` line 3.
  - SQLite version map: spike 005 §Method step 8.
  - sqlite-vec wheel coverage: spike 001 §Method step 9.
  - apsw wheel coverage: spike 001 §Findings → "apsw on macOS".
  - 3.12 as production sweet spot: spike 001 §"Recommended Python stack".

### Option B: Python 3.10+ (broader compatibility)

- Description: `requires-python = ">=3.10"`. Drop to 3.10 to catch a few more host environments.
- Pros:
  - One more minor's worth of compatibility (3.10).
  - Some Linux LTS distros (Ubuntu 22.04) ship 3.10 as default.
- Cons:
  - **Diverges from Hermes.** Hermes pins `>=3.11`. Installing lossless-hermes alongside Hermes still requires 3.11 effectively — declaring 3.10 in our floor is misleading because the combined install fails resolution.
  - **Missing language features.** 3.10 lacks `tomllib`, `Self`, `TypeVarTuple`. We would need `typing_extensions` workarounds or skip these features.
  - 3.10 reaches EOL in October 2026 — same year we ship v0.1. Pinning a floor that's about to EOL is bad hygiene.
  - SQLite 3.37 in Python 3.10 still clears the FTS5 trigram floor (3.34), so this isn't a tech blocker — it's purely a compatibility-with-Hermes question.
- Evidence cited:
  - Python 3.10 EOL: PEP 619, October 2026.
  - Hermes floor mismatch: `/Volumes/LEXAR/Claude/hermes-agent/pyproject.toml` line 10.

### Option C: Python 3.12+ (tighter floor, force modern)

- Description: `requires-python = ">=3.12"`. Lock to 3.12 as the minimum.
- Pros:
  - PEP-695 type-statement syntax available unconditionally.
  - SQLite 3.43+ floor (richer JSON support, faster query planner).
  - Per-interpreter GIL enables future parallelism work.
- Cons:
  - **Excludes Python 3.11 hosts.** Ubuntu 24.04 ships 3.12, but Ubuntu 22.04 LTS (still common) ships 3.10 only — operators would need pyenv. macOS Homebrew users on `python@3.11` would be excluded.
  - **Above Hermes's floor.** Hermes accepts 3.11; we'd be tighter than the host, which is awkward (operators may have 3.11-only Hermes installs that can't install us).
  - No load-bearing 3.12 feature we actually need at v0.1.
- Evidence cited:
  - Ubuntu 22.04 default Python: 3.10. Ubuntu 24.04: 3.12.
  - Hermes floor mismatch: `/Volumes/LEXAR/Claude/hermes-agent/pyproject.toml` line 10.

## Decision

Chosen: **Option A — `requires-python = ">=3.11"`. Document Python 3.12 as the recommended sweet spot.**

## Rationale

The dominant constraint is alignment with Hermes. Operators install both packages into the same environment (per ADR-001's entry-point model), and a mismatched floor breaks resolution. Hermes is at `>=3.11` (`/Volumes/LEXAR/Claude/hermes-agent/pyproject.toml` line 10, also cited in `dependencies.md` line 3) — matching it is the path of least surprise.

Every spike-verified capability lands inside the 3.11+ envelope:

- Spike 001: stdlib `sqlite3` loads `sqlite-vec` on Homebrew Python 3.12.13 and 3.14.3.
- Spike 005: stdlib `sqlite3` provides FTS5 + trigram on every Python ≥ 3.11 (CPython-bundled SQLite ≥ 3.39 vs the 3.34 floor for trigram).
- `sqlite-vec` wheels cover Python 3.10-3.14 with a single `py3-none-*` wheel (spike 001 §Method step 9).
- `apsw` wheels cover Python 3.10-3.13 across our platform matrix.

Python 3.12 is documented as the sweet spot per spike 001 §"Recommended Python stack" (mature, fast, broadly available via Homebrew / pyenv / uv).

Option B (3.10+) was rejected because it diverges from Hermes and the floor is about to EOL (October 2026, the year we ship).

Option C (3.12+) was rejected because no load-bearing 3.12-only feature is in v0.1 scope, and tightening above Hermes's floor creates a needless install-resolution friction.

## Consequences

- **`pyproject.toml` declares `requires-python = ">=3.11"`.** Matches Hermes verbatim.
- **CI matrix runs `{3.11, 3.12, 3.13}` at minimum.** First PR adds the matrix to convert every "inferred" platform cell in `dependencies.md` line 144-160 into "tested".
- **`tomllib` is available unconditionally.** Configuration parsing for any TOML-format LCM files (if introduced later) does not require an external dep.
- **Modern type hints are first-class.** `Self`, `TypeVarTuple`, `LiteralString`, `assert_never` usable without `typing_extensions` in `ty`-checked code.
- **3.12 is the documented recommendation** in README and CONTRIBUTING (best performance per spike 001 §"Recommended Python stack").
- **Apple `/usr/bin/python3` (3.9.6) is below the floor and unsupported** (also unsupported by ADR-004 for a different reason — no `enable_load_extension`).
- **Python 3.10 hosts are excluded.** Ubuntu 22.04 default Python users must install Python 3.11+ via pyenv, deadsnakes PPA, or uv.
- **Forward compatibility is open-ended.** No upper bound; 3.13 and 3.14 are spike-tested and pass.
- **Precluded:** PEP-695 type-statement syntax (`type X = ...`) — works in 3.12+ but not 3.11. Use `TypeAlias` for compatibility with the floor.
- **Invariant:** every `.py` file in the codebase must parse and type-check on Python 3.11. `ty.environment.python-version = "3.13"` in `pyproject.toml` (per `dependencies.md` line 127) sets the **checker's** target — but runtime must run on the declared floor.

## Open questions / 5% uncertainty

1. **`apsw==3.53.1.0` Python 3.14 wheel.** Spike 001 confirmed Python 3.10-3.13 wheels for apsw. Python 3.14 was tested for stdlib sqlite3 (PASS) but not for apsw. If an operator on 3.14 opts into the `[apsw]` extra, they may need `apsw>=3.54.0` (when released). Mitigation: document the gap; bump the pin when 3.14 wheels ship; stdlib path on 3.14 is unaffected.
2. **`ty` Python-version target vs runtime target.** `pyproject.toml` declares `requires-python = ">=3.11"` but `ty.environment.python-version = "3.13"` (per `dependencies.md` line 127, matching Hermes). The checker will accept 3.13-only constructs that don't run on 3.11. Mitigation: CI lints with `ruff target-version = "py311"` (matches the floor); ruff catches stdlib API uses that didn't exist in 3.11.
3. **PEP 619 EOL of 3.11.** Python 3.11 reaches EOL in October 2027. We may want to bump to 3.12+ during v0.x lifecycle. Tracked as a future-bump candidate; no action for v0.1.
4. **PyPy / Jython / IronPython.** Out of scope. `sqlite-vec` and `apsw` both target CPython explicitly. Document but don't engineer for alternative implementations.
