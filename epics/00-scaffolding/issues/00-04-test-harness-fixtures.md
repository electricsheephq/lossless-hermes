---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-00] scaffolding: test harness — _matchers.py + conftest.py fixture skeleton'
labels: 'port, scaffolding, tests'
---

## Source (TypeScript)
- File: `lossless-claw/test/v41-mock-llm.ts`, `lossless-claw/test/v41-test-corpus.ts`, `lossless-claw/test/v41-tool-harness.ts`, scattered uses of `expect.any(...)` / `expect.objectContaining({...})` / `expect.stringContaining(...)` across 113 test files
- Lines: ~217 asymmetric-matcher uses across the TS suite (per ADR-028 §Context); ~4 fixture files
- Function(s)/class(es): N/A — this issue creates the **harness scaffolding**, not the fixtures themselves. Fixtures port in subsequent epics as their target subsystems land.

## Target (Python)
- File: `tests/_matchers.py`, `tests/conftest.py`, `tests/__init__.py` (empty), `tests/fixtures/__init__.py` (empty)
- Estimated LOC: ~80-100 LOC (matchers) + ~60-80 LOC (conftest skeleton)

## Dependencies
- Depends on: #00-01 (needs `[dev]` extra installed: `pytest==9.0.2`, `pytest-asyncio==1.3.0`, `pytest-mock==3.14.0`)
- Blocks: every test-port issue in Epic 01-09 (all tests use `_matchers.py` for asymmetric assertions and `conftest.py` for the in-memory DB fixture)

## Acceptance criteria
- [ ] `tests/_matchers.py` exists with the five asymmetric matcher classes from ADR-028 §Decision point 6 verbatim:
  - [ ] `AnyOf(cls)` — replaces vitest's `expect.any(Cls)` (217+ uses across the TS suite)
  - [ ] `ContainsObject(expected_dict)` — replaces `expect.objectContaining({...})`
  - [ ] `ContainsString(substr)` — replaces `expect.stringContaining("...")`
  - [ ] `ContainsArray(expected_list)` — replaces `expect.arrayContaining([...])`
  - [ ] `MatchesString(pattern)` — replaces `expect.stringMatching(/.../)`
- [ ] Each matcher class implements `__eq__(other)` and `__repr__(self)` so that `assert actual == AnyOf(int)` produces a readable failure message.
- [ ] Module docstring cites ADR-028 §Decision point 6 as the source.
- [ ] `tests/conftest.py` exists with a fixture skeleton (each fixture body may be `raise NotImplementedError` for v0 — the **shapes** are what this issue locks down):
  - [ ] `tmp_home(tmp_path) -> Path` — yields a sandboxed `HERMES_HOME` directory; sets `HERMES_HOME` env var for the duration of the test (per `docs/porting-guides/tests-and-config.md` §"Common fixtures" lines 152-222).
  - [ ] `db_in_memory() -> sqlite3.Connection` — opens an `:memory:` SQLite connection (full migration ladder lands in Epic 01; v0 is the connection only).
  - [ ] `db_with_vec0(db_in_memory) -> sqlite3.Connection` — loads `sqlite-vec` extension into the connection; skip-on-fail with `pytest.skip("sqlite-vec not loadable")` per ADR-028 §Decision point 8.
  - [ ] `fake_voyage() -> respx.MockRouter` — placeholder for the Voyage HTTP mock (`respx==0.21.1`); ports in Epic 05.
  - [ ] `fake_llm() -> object` — placeholder for the deterministic LLM mock (`v41-mock-llm.ts` → `tests/fixtures/mock_llm.py`); ports in Epic 04.
  - [ ] `test_corpus() -> dict` — placeholder for the test conversation corpus (`v41-test-corpus.ts`); ports in Epic 03.
- [ ] `tests/__init__.py` exists (empty) so `tests/` is importable.
- [ ] `tests/fixtures/__init__.py` exists (empty) for future fixture ports.
- [ ] `pyproject.toml` `[tool.pytest.ini_options]` block declares `testpaths = ["tests"]`, `asyncio_mode = "auto"`, and the three marker entries: `live`, `live_voyage`, `live_llm` (per ADR-028 §Consequences).
- [ ] A trivial smoke test `tests/test_smoke.py` asserts `AnyOf(int) == 42` and `ContainsString("hello") == "hello world"` to prove the matcher classes work.
- [ ] `pytest -m 'not live'` runs to completion (the only test is the smoke); CI matrix in #00-02 picks this up.
- [ ] No `_matchers.py` import lands inside `src/lossless_hermes/` — these are test-only.

## Estimated effort
5 hours

## Confidence
95% — the matcher classes are reproduced verbatim from ADR-028 §Decision point 6 (which itself was vetted to be idiomatic Python). Residual risk is in the fixture *shapes* — once Epic 01 actually wires the DB, a shape may need to change. Acceptable; the conftest is iterable.

## Files to read before starting
- `docs/adr/028-vitest-to-pytest.md` (entire ADR — §Decision point 6 has the matcher source code; §Consequences has the conftest fixture list)
- `docs/porting-guides/tests-and-config.md` §"Common fixtures" lines 152-222 (fixture shapes and use-sites)
- `docs/porting-guides/tests-and-config.md` §"Vitest → pytest translation" lines 73-144 (matcher translation table)
- `docs/reference/dependencies.md` lines 38-45 ([dev] extra deps — pytest, pytest-asyncio, pytest-mock, respx)
