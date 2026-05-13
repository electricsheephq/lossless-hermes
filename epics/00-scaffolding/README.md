# Epic 00 — Scaffolding

Project setup before any LCM behavior is ported. Result: a green-CI Python distribution that registers with Hermes as a no-op `ContextEngine`, so an operator running `hermes` with `context.engine: lcm` selected gets a clean startup and a round-trip-passthrough engine. Every subsequent epic lands on top of this.

## Goal

A `pip install -e .`-able Python package, named `lossless-hermes`, that:

1. Builds clean (`uv sync --locked`, `uv build`).
2. Registers a plugin entry point `lossless-hermes = lossless_hermes:register` under the canonical `hermes_agent.plugins` group (ADR-001).
3. Exposes `LCMEngine`, a `ContextEngine` subclass whose `compress(messages)` returns `messages` verbatim and whose `.name = "lcm"`.
4. Passes a CI matrix of `{ubuntu-latest, macos-latest} × {python-3.11, 3.12, 3.13}` running `ruff check`, `ty check`, and `pytest -m 'not live'`.
5. Lets a contributor run `pip install -e ".[dev]" && hermes` (with `context.engine: lcm` in `~/.hermes/config.yaml`) and see Hermes select LCM as the active engine with no LCM-side errors.

This epic ports zero LCM behavior. It is the foundation that makes every later epic measurable — once Epic 00 is green, "did Epic 01 break anything?" reduces to "did CI go red?".

## Deliverables

| Artifact | Path | Issue |
|---|---|---|
| `pyproject.toml` with deps & build config | `pyproject.toml` | #00-01 |
| `src/` layout + package skeleton | `src/lossless_hermes/__init__.py` | #00-01 |
| CI matrix workflow | `.github/workflows/ci.yml` | #00-02 |
| `pre-commit` hooks | `.pre-commit-config.yaml` | #00-03 |
| Test harness + fixtures | `tests/conftest.py`, `tests/_matchers.py` | #00-04 |
| Hermes bridge shim | `src/lossless_hermes/hermes_bridge.py` | #00-05 |
| No-op `LCMEngine` | `src/lossless_hermes/engine/__init__.py` | #00-06 |
| Pydantic v2 config skeleton | `src/lossless_hermes/db/config.py` | #00-07 |
| Top-level README + quickstart | `README.md` | #00-08 |

## Dependencies

**None.** This is the entry point of the port. Phase 1 (architecture & planning) is the only upstream artifact — the ADRs and porting guides under `docs/` are read-only inputs.

## Blocks

**Epic 01 (Storage).** Without a built package, no `open_lcm_db()` has anywhere to land. Epic 01 starts by adding `src/lossless_hermes/db/connection.py` to the skeleton this epic produces.

Transitively blocks every other epic (02-09).

## Critical path

**YES.** Every other epic waits on this one. There is no parallelizable work that does not depend on the package skeleton existing.

## Estimated total effort

**1 week — ~30-40 hours** of focused work for one engineer:

- 6 h — `pyproject.toml` + src layout (#00-01)
- 5 h — CI matrix (#00-02)
- 3 h — pre-commit hooks (#00-03)
- 5 h — test harness + fixtures (#00-04)
- 3 h — Hermes bridge stub (#00-05)
- 4 h — no-op engine + plugin registration (#00-06)
- 4 h — config skeleton (#00-07)
- 4 h — README + docs (#00-08)
- 4-6 h — slack for first-CI-green debugging across the matrix

Most of the time is debugging CI on macOS vs Linux and on Python 3.11/3.12/3.13 simultaneously — every "inferred" platform cell in `docs/reference/dependencies.md` lines 144-160 has to become "tested" by the end of this epic.

## Confidence

**95%.** Every ADR referenced below is at 95%+ confidence. The 5% residual:

- `apsw==3.53.1.0` may not have a Python 3.14 wheel (ADR-005 §"Open questions" #1). Not load-bearing — stdlib `sqlite3` is the primary backend, `apsw` is opt-in.
- `ty==0.0.21` is pre-1.0; a bump may shift config syntax (ADR-008 §"Open questions" #1). Mitigated by exact pinning + `[type-mypy]` fallback.
- Plugin discovery silently skips on import error (ADR-001 §"Open questions" #3). Mitigated by the startup health-check in #00-06.

## Issues

| # | Title | Hours | Confidence | Depends on |
|---|---|---:|---:|---|
| [#00-01](./issues/00-01-pyproject-and-package-skeleton.md) | pyproject.toml + package skeleton | 6 | 95% | — |
| [#00-02](./issues/00-02-ci-matrix.md) | GitHub Actions CI matrix | 5 | 90% | #00-01 |
| [#00-03](./issues/00-03-precommit-hooks.md) | pre-commit hooks | 3 | 95% | #00-01 |
| [#00-04](./issues/00-04-test-harness-fixtures.md) | tests/ harness + asymmetric matchers | 5 | 95% | #00-01 |
| [#00-05](./issues/00-05-hermes-bridge-stub.md) | hermes_bridge.py shim | 3 | 90% | #00-01 |
| [#00-06](./issues/00-06-noop-engine.md) | no-op LCMEngine + register(ctx) | 4 | 90% | #00-01, #00-05 |
| [#00-07](./issues/00-07-config-skeleton.md) | pydantic v2 LcmConfig skeleton | 4 | 95% | #00-01 |
| [#00-08](./issues/00-08-readme-and-docs.md) | top-level README + quickstart | 4 | 95% | #00-01..#00-07 |

## Exit criteria

Epic 00 is done when **all eight issues are merged AND the following one-liner produces a clean session**:

```sh
uv pip install -e ".[dev]"
# ~/.hermes/config.yaml contains:
#   context: { engine: lcm }
#   plugins: { enabled: [lossless-hermes] }
hermes  # session starts; LCM engine is selected; compress() is a passthrough.
```

When this works on at least one operator's machine and CI is green on all 6 matrix cells, Epic 01 is unblocked.
