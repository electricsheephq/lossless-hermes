# ADR-028: Vitest → Pytest translation

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

`lossless-claw` ships **113 test files containing 1,595 `it()`/`test()` cases** (vitest, TypeScript) at `/Volumes/LEXAR/Claude/lossless-claw/test/` (`docs/porting-guides/tests-and-config.md` lines 1–8). The suite includes:

- Heavy mocking via `vi.fn()` (engine.test.ts uses ~50 inline mocks), `vi.mock("@mariozechner/pi-ai", ...)`, and `vi.hoisted()`.
- 4 fixture files (`v41-mock-llm.ts`, `v41-test-corpus.ts`, `v41-stress-corpus.ts`, `v41-tool-harness.ts`).
- Conditional skipping via `describe.skipIf(!VEC0_AVAILABLE)` for sqlite-vec-gated suites.
- 0 snapshot tests (verified — no `toMatchSnapshot` or `toMatchInlineSnapshot`).
- ~217 asymmetric matcher uses (`expect.any(...)`, `expect.objectContaining({...})`, `expect.stringContaining("...")`).

The constraint forcing a choice: how aggressively should we restructure the test suite during the port? Three axes: file structure (1:1 vs. consolidated), test-case count (1:1 vs. `parametrize`-collapsed), and matcher style (asymmetric helpers vs. hand-rolled).

`docs/porting-guides/tests-and-config.md` §"Vitest → pytest translation" provides a complete translation table (lines 73–144) for matchers, mocking styles, lifecycle hooks, and asymmetric matchers. The work itself is well-scoped; what we decide here is the port strategy.

## Options considered

### Option A: 1:1 file-by-file port preserving test names

- Description: Every `test/foo.test.ts` becomes `tests/test_foo.py`. Every `it("desc", ...)` becomes `def test_desc():` (or `async def test_desc():` for async). Test names are deterministic translations. `describe(...)` blocks become Python classes (`class TestEngine:` for `describe("Engine", ...)`).
- Pros:
  - **Mechanical port surface.** A contributor can claim "I'll port the storage tests" and have a deterministic file list.
  - **1:1 mapping is verifiable.** Test-count comparison (Python vs. TS) becomes a port-completeness metric — file `engine.test.ts` has 228 cases; `tests/test_engine.py` should have ~228 (modulo parametrize collapses).
  - **Parallel porting work alongside each subsystem port.** A contributor working on `store/conversation.py` can port `test/store/conversation-store.test.ts` independently.
  - **Test-discovery via `tests/**/test_*.py` is standard pytest.** No special config beyond `testpaths = ["tests"]` in `pyproject.toml`.
  - **Failure messages stay actionable.** A `test_engine_bootstrap_imports_jsonl_history_with_size_match` failure points at a specific TS test case to compare behavior.
- Cons:
  - Some TS files split into multiple Python files for readability (e.g. `v41-adversarial-scenarios.test.ts` with 37 cases might split). Acceptable; documented per-file.
  - `expect(...).rejects.toThrow()` chains often need to split into 2 cases in Python — total count drifts (`docs/porting-guides/tests-and-config.md` "Remaining 5% risk" #7).
- Evidence cited:
  - `docs/porting-guides/tests-and-config.md` §"Subsystem table" lines 22–40 — already maps every TS test file to a Python target file.
  - `docs/porting-guides/tests-and-config.md` §"Vitest → pytest translation" lines 73–144 — full translation table.

### Option B: Subsystem-consolidated port

- Description: Each subsystem gets ONE Python test file consolidating all related TS files. `tests/test_storage.py` covers everything in the `store/` directory.
- Pros: Fewer Python files; arguably cleaner directory structure.
- Cons:
  - Loses 1:1 mapping. Bug-hunts that start "I'm looking at engine.test.ts case 47" become harder.
  - Some consolidated files would be 3000+ LOC. Test review suffers.
  - Tests-and-config.md's subsystem table maps files 1:1 already; ignoring that wastes the planning work.

### Option C: Test-name-driven port (rename freely)

- Description: Use porting as an opportunity to rename tests for clarity. `it("rejects malformed JSON")` → `def test_malformed_json_raises_value_error():`.
- Pros: Resulting names might be more idiomatic Python.
- Cons:
  - Breaks the cross-reference link to TS source. Future LCM patch back-ports require manual case-by-case matching.
  - High judgment overhead during port — every test name is a new design decision.
  - Renaming for renaming's sake is busywork; preserving names is cheaper and reversible.

## Decision

Chosen: **Option A — 1:1 file-by-file port preserving test names**.

Conventions:

1. **File structure mirror.** `test/foo.test.ts` → `tests/test_foo.py`. For nested directories like `test/tools/`, use `tests/tools/test_*.py`. Subsystem mapping is documented in `docs/porting-guides/tests-and-config.md` §"Subsystem table" lines 22–40.

2. **Test name translation rule.** `it("does the thing", ...)` becomes `def test_does_the_thing():` — lowercase, snake_case, spaces→underscores, drop punctuation. For async tests, prefix `@pytest.mark.asyncio` (or rely on `asyncio_mode = "auto"` in `pyproject.toml` — recommended).

3. **`describe(...)` blocks** become Python classes. `describe("LcmContextEngine.ingest", () => { ... })` → `class TestLcmContextEngineIngest:`. Nested `describe` becomes nested class.

4. **Async tests.** Use `pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml`. Then `async def test_thing():` works without per-test markers. Matches the existing recommendation in `docs/porting-guides/tests-and-config.md` §"Tooling stack" line 542.

5. **Matcher translation per the table** in `docs/porting-guides/tests-and-config.md` §"Core assertion table" + §"Matchers (the big-15)" + §"Async assertions".

6. **Asymmetric matchers via `tests/_matchers.py`.** Centralize the 217+ asymmetric uses (`expect.any(String)`, `expect.objectContaining({...})`, etc.) behind a small helper module:

   ```python
   # tests/_matchers.py
   """Asymmetric matchers for porting vitest's expect.any / expect.objectContaining.

   Each class implements __eq__ so it compares true against any value matching the
   declared shape. Use them in equality assertions:

       assert actual == {"role": "user", "ts": AnyOf(int), "msg": ContainsString("hello")}

   This replaces vitest's:

       expect(actual).toEqual({ role: "user", ts: expect.any(Number),
                               msg: expect.stringContaining("hello") })

   See ADR-028 §"Decision" point 6.
   """

   from typing import Any, Type


   class AnyOf:
       """Matches any value of the given type. Replaces vitest's expect.any(Cls)."""
       def __init__(self, cls: Type[Any]) -> None:
           self._cls = cls

       def __eq__(self, other: Any) -> bool:
           return isinstance(other, self._cls)

       def __repr__(self) -> str:
           return f"AnyOf({self._cls.__name__})"


   class ContainsObject:
       """Subset-equality for dicts. Replaces vitest's expect.objectContaining({...})."""
       def __init__(self, expected: dict) -> None:
           self._expected = expected

       def __eq__(self, other: Any) -> bool:
           if not isinstance(other, dict):
               return False
           return all(other.get(k) == v for k, v in self._expected.items())

       def __repr__(self) -> str:
           return f"ContainsObject({self._expected!r})"


   class ContainsString:
       """Substring-match. Replaces vitest's expect.stringContaining("...")."""
       def __init__(self, substr: str) -> None:
           self._substr = substr

       def __eq__(self, other: Any) -> bool:
           return isinstance(other, str) and self._substr in other

       def __repr__(self) -> str:
           return f"ContainsString({self._substr!r})"


   class ContainsArray:
       """Subset-match for lists. Replaces vitest's expect.arrayContaining([...])."""
       def __init__(self, expected: list) -> None:
           self._expected = expected

       def __eq__(self, other: Any) -> bool:
           if not isinstance(other, list):
               return False
           return all(item in other for item in self._expected)


   class MatchesString:
       """Regex-match. Replaces vitest's expect.stringMatching(/x/)."""
       def __init__(self, pattern: str) -> None:
           import re
           self._pattern = re.compile(pattern)

       def __eq__(self, other: Any) -> bool:
           return isinstance(other, str) and bool(self._pattern.search(other))
   ```

7. **Mocking translation.**
   - `vi.fn(async () => ...)` → `unittest.mock.AsyncMock(...)` (or `pytest_mock`'s `mocker.AsyncMock()`).
   - `vi.mock("module", ...)` → `monkeypatch.setattr("module.symbol", replacement)`.
   - `vi.spyOn(obj, "method")` → `mocker.spy(obj, "method")` or `unittest.mock.patch.object(obj, "method", wraps=obj.method)`.
   - `vi.hoisted(...)` collapses to a plain `monkeypatch.setattr` in a fixture (Python has no hoisting problem).

8. **Conditional skipping.** `describe.skipIf(!VEC0_AVAILABLE)` translates to `pytestmark = pytest.mark.skipif(not _vec0_available(), reason="sqlite-vec not loadable")` at module top OR class scope (per `docs/porting-guides/tests-and-config.md` §"Remaining 5% risk" #5 — vitest's skipIf is `describe`-block granularity; pytest's `pytestmark` matches that scope).

9. **Fixtures** in `tests/conftest.py` following the patterns at `docs/porting-guides/tests-and-config.md` §"Common fixtures" lines 152–222: `tmp_home`, `db_in_memory`, `db_with_vec0`, `fake_voyage`, `fake_llm`, `test_corpus`.

10. **Live-integration tests** behind `pytest.mark.live` opt-in. Two suites: `live_voyage` (needs `VOYAGE_API_KEY`), `live_llm` (needs Anthropic/provider key). Default `pytest` skips both. CI runs the live suite on `main` pushes only (per the recommended CI matrix at `docs/porting-guides/tests-and-config.md` lines 498–531).

11. **Coverage target: 80% line.** No per-module gates. Mirrors LCM's apparent ~85% logical coverage without holding the port hostage to a higher bar (per `docs/porting-guides/tests-and-config.md` §"Open decisions / ADRs to write" line 553).

## Rationale

The 1:1 strategy makes parallel porting work feasible. With 113 source files and a clear 1:1 target table in `docs/porting-guides/tests-and-config.md` §"Subsystem table", contributors can pick up a file, port it, and merge independently — no global coordination needed. The result is verifiable: test count is within 10–20% of the source (`docs/porting-guides/tests-and-config.md` "Remaining 5% risk" #7 calls out the expected drift from parametrize collapses and `rejects.toThrow` splits).

The asymmetric-matcher helper module is the highest-leverage convenience: 217+ uses across the suite would otherwise be hand-rolled per-test, with drift across files. Centralizing makes the assertions readable.

Subsystem-consolidated (Option B) loses the cross-reference to TS source and produces unreadable mega-files. Test-name renaming (Option C) is busywork with no payoff.

The `_matchers.py` module is small enough (~80 LOC) that it does not introduce a new abstraction layer — it's a Python idiom-fit for vitest's symmetric `expect.X` API.

## Consequences

- **`tests/` directory structure:** mirrors `test/` from LCM. Subsystem subdirs (`tests/tools/`, `tests/embeddings/`, etc.) per `docs/porting-guides/tests-and-config.md` §"Subsystem table".
- **`tests/conftest.py`** provides `tmp_home`, `db_in_memory`, `db_with_vec0`, `fake_voyage`, `fake_llm`, `test_corpus`. Imported automatically by pytest.
- **`tests/_matchers.py`** is shared across all test files for asymmetric matching.
- **`pyproject.toml`** configuration:
  ```toml
  [tool.pytest.ini_options]
  testpaths = ["tests"]
  asyncio_mode = "auto"
  markers = [
      "live: live API integration tests (requires API keys; opt-in)",
      "live_voyage: live Voyage API tests",
      "live_llm: live LLM provider tests",
  ]
  ```
- **`pytest-asyncio`** is a dev dependency; `pytest-mock` is a dev dependency.
- **Fixture files:** `tests/fixtures/mock_llm.py`, `tests/fixtures/test_corpus.py`, `tests/fixtures/stress_corpus.py`, `tests/fixtures/tool_harness.py`. Port priority listed in `docs/porting-guides/tests-and-config.md` §"Common fixtures" line 224.
- **`v41-mock-llm.ts` → `tests/fixtures/mock_llm.py`** ports the deterministic-shape mock with its `good | fabricated_citations | malformed_json | hallucinated_content | empty | throw | rate_limit | verify_OK/HALLUCINATION/UNSUPPORTED` repertoire.
- **CI matrix:** Python 3.11 + 3.12 across ubuntu-latest + macos-latest. Live-Voyage as a separate job, gated on `main` pushes only. Codecov integration. Exact YAML at `docs/porting-guides/tests-and-config.md` §"Recommended Hermes CI" lines 498–531.
- **Coverage target: 80% line.** No per-module gates. Codecov reports without failing CI on shortfall.
- **Invariant:** every TS test case maps to at least one Python test case. Drift > 20% requires investigation (likely a missing port).
- **Invariant:** snapshot tests are NOT introduced in v0.1. LCM doesn't use them; introducing them during a port adds maintenance load without correctness gain.

## Open questions / 5% uncertainty

1. **`vi.hoisted()` import-order trick** used in 3 files (`summarize.test.ts`, `circuit-breaker.test.ts`, `lcm-summarizer-reasoning.test.ts`). Python doesn't have the hoist problem, but tests that *relied* on the hoist need careful translation — usually they collapse to a `monkeypatch.setattr` in a fixture. Flag during the actual port; not a general policy decision (`docs/porting-guides/tests-and-config.md` §"Remaining 5% risk" #4).
2. **Coverage parity is hard to measure.** Vitest doesn't run coverage on LCM's main branch, so there's no concrete baseline. We pick 80% line as a stretch target; revisit once Python suite has a baseline (`docs/porting-guides/tests-and-config.md` §"Remaining 5% risk" #8).
3. **THE_FIVE_QUESTIONS predicate language.** `v41-five-questions.test.ts` uses a `predicate: (response) => string | null` returning null (PASS) or an error string (FAIL). Porting via bare `assert` swallows the human-readable failure message. Keep a tiny predicate helper (`tests/_predicate.py`) that mirrors the contract (`docs/porting-guides/tests-and-config.md` §"Remaining 5% risk" #6).
4. **SQLite extension loading on Python.** `sqlite3.enable_load_extension` is disabled by default on Homebrew Python on macOS. Either ship `pysqlite3-binary` as a dev dep or document the install ritual. Decision deferred to a dependency ADR (`docs/porting-guides/tests-and-config.md` §"Remaining 5% risk" #2).
5. **`describe.skipIf` granularity.** Vitest skips at `describe` block; pytest's `pytest.mark.skipif` works per-test or via `pytestmark` at module scope. Mixed-gated test files (`embeddings-store.test.ts`: always-run cases plus vec0-gated `describe.skipIf`) need careful class-level skipping to preserve semantics (`docs/porting-guides/tests-and-config.md` §"Remaining 5% risk" #5).
