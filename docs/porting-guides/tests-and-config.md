# Porting Guide: Tests + Configuration

**Source test files:** 113 (vitest, TypeScript) at `/Volumes/LEXAR/Claude/lossless-claw/test/`
**Source test count:** **1595 `it()`/`test()` cases** (raw grep on `^[[:space:]]*\(it\|test\)(`)
**Python test target:** pytest, **~1595 tests** (1:1 port target; some collapse via parametrize, some split for `pytest.raises`)
**Confidence target:** 95% (some Voyage-/vec0-gated suites are conditional in source — they remain conditional)
**Estimated effort:** 40–60 hours of focused work, runnable in parallel with each subsystem port. Most files are mechanical; the heavy lifters are `engine.test.ts` (228 tests), `lcm-integration.test.ts` (77), `config.test.ts` (61), `expansion-auth.test.ts` (50), and `summarize.test.ts` (49).

---

## Test inventory by subsystem

### Headline numbers
- 113 test files
- 1595 tests
- 4 fixture files (`test/fixtures/*.ts`) — all hand-written TS, no JSON/snapshot fixtures
- 0 snapshot tests (no `toMatchSnapshot` / `toMatchInlineSnapshot` anywhere)
- Heavy use of `describe.skipIf(!VEC0_AVAILABLE)` for sqlite-vec-gated suites (auto-skip in CI)

### Subsystem table

| Subsystem | TS test files (test/*.test.ts) | Tests | Python target file |
|---|---|---:|---|
| **Config / manifest** | `config.test.ts`, `manifest.test.ts`, `plugin-config-registration.test.ts`, `resolve-model-api-from-runtime-config.test.ts` | 104 | `tests/test_config.py`, `tests/test_manifest.py`, `tests/test_plugin_config.py`, `tests/test_resolve_model.py` |
| **DB / migration** | `db-connection.test.ts`, `migration.test.ts`, `v41-indexes.test.ts`, `v41-schema-drift-invariants.test.ts`, `v41-pre-existing-schema-migration.test.ts`, `v41-support-tables.test.ts` | 51 | `tests/test_db_connection.py`, `tests/test_migration.py`, `tests/test_schema_invariants.py` |
| **Storage (conversation + summary)** | `compaction-maintenance-store.test.ts`, `summary-store.test.ts`, `fts-fallback.test.ts`, `fts5-sanitize.test.ts`, `parse-utc-timestamp.test.ts`, `message-identity.test.ts`, `v41-summaries-columns.test.ts`, `transcript-repair.test.ts` | 48 | `tests/test_conversation_store.py`, `tests/test_summary_store.py`, `tests/test_fts.py`, `tests/test_transcript_repair.py` |
| **Engine (orchestration)** | `engine.test.ts`, `lcm-integration.test.ts`, `regression-2026-03-17.test.ts`, `bootstrap-message-only.test.ts`, `bootstrap-flood-regression.test.ts`, `v41-fixture-smoke.test.ts`, `v41-wiring.test.ts`, `v41-tool-wiring-smoke.test.ts` | 380 | `tests/test_engine.py`, `tests/test_engine_integration.py`, `tests/test_bootstrap.py`, `tests/test_engine_wiring.py` |
| **Assembler / prune** | `assembler-blocks.test.ts`, `prune.test.ts`, `estimate-tokens.test.ts`, `large-files.test.ts`, `vitest-isolation.test.ts` | 82 | `tests/test_assembler.py`, `tests/test_prune.py`, `tests/test_estimate_tokens.py`, `tests/test_large_files.py` |
| **Compaction / summarize** | `summarize.test.ts`, `circuit-breaker.test.ts`, `cache-aware-deferral-gate.test.ts`, `custom-instructions.test.ts`, `extract-auth-failure.test.ts`, `lcm-summarizer-reasoning.test.ts`, `v41-needs-compact-gate.test.ts`, `v41-lcm-compact-tool.test.ts`, `v41-leaf-cap.test.ts`, `v41-token-state.test.ts`, `v41-tool-budget-guardrail.test.ts` | 153 | `tests/test_summarize.py`, `tests/test_circuit_breaker.py`, `tests/test_cache_aware_deferral.py`, `tests/test_custom_instructions.py`, `tests/test_compaction_gate.py`, `tests/test_leaf_cap.py`, `tests/test_token_state.py` |
| **Expansion** | `expansion.test.ts`, `expansion-auth.test.ts`, `expansion-policy.test.ts` | 63 | `tests/test_expansion.py`, `tests/test_expansion_auth.py`, `tests/test_expansion_policy.py` |
| **Tools (8 agent tools)** | `lcm-grep-tool-hybrid.test.ts`, `lcm-grep-verbatim-mode.test.ts`, `lcm-describe-expand-flags.test.ts`, `lcm-expand-query-tool.test.ts`, `lcm-expand-tool.test.ts`, `lcm-expand-tool.delegation.test.ts`, `lcm-get-entity-tool.test.ts`, `lcm-search-entities-tool.test.ts`, `lcm-synthesize-around-tool.test.ts`, `lcm-tools.test.ts`, `v41-tool-parity-invariants.test.ts` | 113 | `tests/tools/test_grep.py`, `tests/tools/test_describe.py`, `tests/tools/test_expand.py`, `tests/tools/test_expand_query.py`, `tests/tools/test_get_entity.py`, `tests/tools/test_search_entities.py`, `tests/tools/test_synthesize_around.py` |
| **Embeddings / Voyage** | `embeddings-store.test.ts`, `embeddings-backfill.test.ts`, `voyage-client.test.ts`, `semantic-search.test.ts`, `hybrid-search.test.ts`, `retrieval-sort.test.ts`, `v41-embedding-meta-tables.test.ts` | 99 | `tests/embeddings/test_store.py`, `tests/embeddings/test_backfill.py`, `tests/embeddings/test_voyage_client.py`, `tests/embeddings/test_semantic_search.py`, `tests/embeddings/test_hybrid_search.py`, `tests/embeddings/test_retrieval_sort.py` |
| **Synthesis** | `synthesis-dispatch.test.ts`, `synthesis-prompt-registry.test.ts`, `v41-synthesis-tables.test.ts`, `v41-synthesis-quality.test.ts`, `v41-seed-default-prompts.test.ts`, `v41-prompt-registry-null-uniq.test.ts` | 71 | `tests/synthesis/test_dispatch.py`, `tests/synthesis/test_prompt_registry.py`, `tests/synthesis/test_synthesis_tables.py`, `tests/synthesis/test_synthesis_quality.py` |
| **Extraction (entities)** | `entity-coreference.test.ts`, `v41-entity-extractor-llm.test.ts`, `v41-entity-layer-tables.test.ts` | 28 | `tests/extraction/test_entity_coreference.py`, `tests/extraction/test_entity_extractor.py`, `tests/extraction/test_entity_tables.py` |
| **Operator / orchestration** | `operator-eval-runner.test.ts`, `operator-health.test.ts`, `operator-purge.test.ts`, `operator-reconcile-session-keys.test.ts`, `operator-worker-orchestrator.test.ts`, `v41-backfill-autostart.test.ts`, `v41-data-cleanup.test.ts`, `v41-qa-runner-antipatterns.test.ts` | 79 | `tests/operator/test_eval_runner.py`, `tests/operator/test_health.py`, `tests/operator/test_purge.py`, `tests/operator/test_reconcile.py`, `tests/operator/test_orchestrator.py`, `tests/operator/test_data_cleanup.py` |
| **Concurrency / workers** | `concurrency-model.test.ts`, `transaction-mutex.test.ts`, `worker-lock.test.ts`, `worker-loop.test.ts`, `lcm-worker-lock.test.ts`, `session-operation-queues.test.ts`, `session-patterns.test.ts`, `v41-concurrency-invariants.test.ts` | 60 | `tests/concurrency/test_model.py`, `tests/concurrency/test_mutex.py`, `tests/concurrency/test_worker_lock.py`, `tests/concurrency/test_worker_loop.py`, `tests/concurrency/test_session_queues.py` |
| **Eval / judge** | `eval-judge.test.ts`, `eval-query-set.test.ts`, `eval-recall.test.ts`, `eval-run.test.ts`, `v41-eval-tables.test.ts` | 61 | `tests/eval/test_judge.py`, `tests/eval/test_query_set.py`, `tests/eval/test_recall.py`, `tests/eval/test_run.py`, `tests/eval/test_eval_tables.py` |
| **Plugin / command / hooks** | `lcm-command.test.ts`, `plugin-prompt-hook.test.ts`, `index-complete-model-auth.test.ts`, `index-complete-options.test.ts`, `index-complete-provider-config.test.ts`, `index-secret-ref-auth-profiles.test.ts` | 61 | `tests/plugin/test_command.py`, `tests/plugin/test_prompt_hook.py`, `tests/plugin/test_index_complete.py`, `tests/plugin/test_secret_ref.py` |
| **v4.1 invariant / adversarial / regression** | `v41-adversarial-output-bounds.test.ts`, `v41-adversarial-scenarios.test.ts`, `v41-authorization-invariants.test.ts`, `v41-cross-module-invariants.test.ts`, `v41-five-questions.test.ts`, `v41-finalreview-suppression.test.ts`, `v41-group-b-fix2.test.ts`, `v41-period-timezone.test.ts`, `v41-stress-fixture.test.ts`, `v41-suppression-cascade-trigger.test.ts`, `v41-suppression-fts-filter.test.ts`, `v41-suppression-invariants.test.ts`, `v41-wave10-reviewer-regressions.test.ts`, `v41-wave12-meta-invariants.test.ts` | 142 | `tests/v41/test_adversarial.py`, `tests/v41/test_authorization.py`, `tests/v41/test_cross_module.py`, `tests/v41/test_five_questions.py`, `tests/v41/test_suppression.py`, `tests/v41/test_period_timezone.py`, `tests/v41/test_regressions.py` |
| **TOTAL** | **113 files** | **1595** | **~70 Python files** (Python tends to consolidate into fewer files per subsystem) |

### Top 10 by test count (the ones that dominate effort)
| File | Tests | Notes |
|---|---:|---|
| `engine.test.ts` | 228 | The orchestration unit — full LcmContextEngine surface. Uses `vi.fn()` extensively for `complete`/`callGateway`/`resolveModel` mocks. |
| `lcm-integration.test.ts` | 77 | End-to-end through real SQLite. Mostly in-memory `:memory:` DBs. |
| `config.test.ts` | 61 | Pure-function test of `resolveLcmConfig(env, pluginConfig)`. Mechanical port — no mocking. |
| `expansion-auth.test.ts` | 50 | Auth-grant lifecycle for `lcm_expand_query` delegated sub-agent. Uses `vi.fn()` + `vi.mocked()`. |
| `summarize.test.ts` | 49 | LLM-call wrapper + fallback chain. Heavy mocking via `vi.mock("@mariozechner/pi-ai", ...)` + `vi.hoisted()`. |
| `assembler-blocks.test.ts` | 44 | DAG-block assembly under a token budget. Pure logic — no mocks. |
| `lcm-command.test.ts` | 39 | `/lcm` CLI command — wires through real engine. |
| `v41-adversarial-scenarios.test.ts` | 37 | THE_FIVE_QUESTIONS-style scenarios against fixture corpus. |
| `lcm-expand-query-tool.test.ts` | 28 | Tool unit — uses `makeTestDeps()` + `makeTestEngine()`. |
| `embeddings-store.test.ts` | 28 | Splits cleanly: dim-mismatch / sluggify tests run always; vec0-backed tests `describe.skipIf(!VEC0_AVAILABLE)`. |

### Conditionally-skipped suites (gating in source)
- **`describe.skipIf(!VEC0_AVAILABLE)`** — appears in `embeddings-store.test.ts`, `embeddings-backfill.test.ts`, `hybrid-search.test.ts`, `semantic-search.test.ts`. The TS check is `existsSync(LCM_TEST_VEC0_PATH || dev-path)`. Python equivalent: `pytest.skipif(not _vec0_available(), reason="sqlite-vec not loadable")` at module scope.
- **`itIfFts5 = detectFts5Support() ? it : it.skip`** — appears in `fts-fallback.test.ts`. SQLite-build-feature dependent. Python: same pattern via `pytest.mark.skipif(not _fts5_available())`.
- **`VOYAGE_API_KEY`-gated** — `voyage-client.test.ts` runs fully mocked. The doc-comment ("no live API calls") states live runs happen only in CI workflows with explicit env gating. Python equivalent: keep mocked unit suite; gate live integration tests behind `pytest.mark.live` + `pytest -m live` opt-in.

### Fixtures inventory (`test/fixtures/`)
| File | Lines | What it provides |
|---|---:|---|
| `v41-mock-llm.ts` | 7,504 bytes (~280 lines) | Deterministic mock `LlmCall` impl with adversarial response shapes (`good`, `fabricated_citations`, `malformed_json`, `hallucinated_content`, `empty`, `throw`, `rate_limit`, `verify_OK/HALLUCINATION/UNSUPPORTED`). |
| `v41-test-corpus.ts` | 33,076 bytes | Synthetic conversation corpus + summaries seeded into a `DatabaseSync(":memory:")`. Exports `buildTestCorpus(db)` and `BASE_DATE`. Drives the THE_FIVE_QUESTIONS scenarios. |
| `v41-stress-corpus.ts` | 34,763 bytes | Larger stress corpus for `v41-stress-fixture.test.ts`. |
| `v41-tool-harness.ts` | 7,008 bytes (~186 lines) | `makeTestDeps()` + `makeTestEngine(db)` factories. Centralizes LcmDependencies + LcmContextEngine mocks so the 8 tool tests don't drift. |

Port these first — they're load-bearing for ~25% of the suite.

---

## Vitest → pytest translation

### Core assertion table
| Vitest | Pytest |
|---|---|
| `import { describe, it, expect } from "vitest"` | `import pytest` (assertions are bare `assert`) |
| `describe("name", () => { ... })` | `class TestName:` or just module-level grouping |
| `it("name", () => { ... })` | `def test_name():` |
| `it("name", async () => { ... })` | `@pytest.mark.asyncio` + `async def test_name():` |
| `it.skip("name", () => { ... })` | `@pytest.mark.skip(reason="...")` |
| `describe.skipIf(!cond)("name", ...)` | `pytestmark = pytest.mark.skipif(not cond, reason="...")` at module top, or scope it to a class |
| `it.skipIf(!cond)("name", ...)` | `@pytest.mark.skipif(not cond, reason="...")` per-test |

### Matchers (the big-15, sorted by frequency — covers ~95% of the file)
| Vitest (count) | Pytest |
|---|---|
| `.toBe(y)` (2058) | `assert x == y` (or `is` for identity) |
| `.toContain(y)` (749) | `assert y in x` (strings/lists/dicts) |
| `.toEqual(y)` (404) | `assert x == y` (deep) — Python's `==` on lists/dicts is structural by default |
| `.toBeNull()` (287) | `assert x is None` |
| `.toHaveLength(n)` (195) | `assert len(x) == n` |
| `.toHaveBeenCalledWith(...)` (159) | `mock.assert_called_with(...)` |
| `.toThrow(/regex/)` (111) | `with pytest.raises(Exception, match=r"regex"): ...` |
| `.toMatchObject({...})` (105) | manual subset assert, or `assert {k: x[k] for k in expected} == expected` |
| `.toBeGreaterThan(n)` / `.toBeGreaterThanOrEqual(n)` (102 / 81) | `assert x > n` / `assert x >= n` |
| `.toMatch(/regex/)` (94) | `assert re.search(r"regex", x)` |
| `.toBeDefined()` (92) | `assert x is not None` (more idiomatic) or `assert "key" in obj` |
| `.toHaveBeenCalled()` (91) | `mock.assert_called()` |
| `.toBeUndefined()` (63) | `assert x is None` (Python has no undefined — use None) |
| `.toHaveBeenCalledTimes(n)` (57) | `assert mock.call_count == n` |
| `.toBeLessThanOrEqual(n)` / `.toBeLessThan(n)` (44 / 32) | `assert x <= n` / `assert x < n` |
| `.toBeCloseTo(n, decimals)` (22) | `assert x == pytest.approx(n, abs=10**-decimals)` |
| `.toBeInstanceOf(Cls)` (11) | `assert isinstance(x, Cls)` |
| `.toBeTruthy()` (10) | `assert x` |
| `.toHaveProperty("key")` (29) | `assert "key" in x` (for dicts) or `hasattr(x, "key")` (for objects) |
| `.toBeTypeOf("string")` (37) | `assert isinstance(x, str)` |

### Asymmetric matchers (the "any of" helpers)
| Vitest | Pytest |
|---|---|
| `expect.any(String)` (used 73x) | Wrap in a helper class with `__eq__` returning `isinstance(other, str)`, OR use `assert isinstance(x, str)` directly |
| `expect.objectContaining({...})` (82x) | Project-supplied helper: `assert {k: actual[k] for k in expected} == expected` |
| `expect.stringContaining("x")` (53x) | `assert "x" in actual` |
| `expect.arrayContaining([...])` (9x) | `assert set(expected).issubset(set(actual))` |
| `expect.stringMatching(/x/)` (4x) | `assert re.search(r"x", actual)` |

**Recommendation:** publish a small `tests/_matchers.py` module with `AnyOf(Cls)`, `ContainsObject(d)`, `ContainsString(s)` — keeps the 217 asymmetric uses readable without hand-rolling each call.

### Async assertions
| Vitest (count) | Pytest |
|---|---|
| `await expect(p).rejects.toThrow(/x/)` (22) | `with pytest.raises(Exception, match=r"x"): await coro` |
| `await expect(p).rejects.toMatchObject({...})` (12) | `with pytest.raises(Exception) as ei: await coro; assert ...ei.value...` |
| `await expect(p).resolves.toBe(y)` (9) | `assert (await coro) == y` |
| `await expect(p).resolves.toEqual(y)` (6) | `assert (await coro) == y` |
| `await expect(p).rejects.toBeInstanceOf(Cls)` (6) | `with pytest.raises(Cls): await coro` |

### Lifecycle hooks
| Vitest | Pytest |
|---|---|
| `beforeEach(() => { ... })` | `@pytest.fixture(autouse=True)` returning a setup value; or `yield`/teardown form |
| `afterEach(() => { ... })` | the teardown block after `yield` in an `autouse=True` fixture |
| `beforeAll(() => { ... })` | `@pytest.fixture(scope="module", autouse=True)` |
| `afterAll(() => { ... })` | same fixture's teardown after `yield` |

### Mocking
LCM uses three styles, each gets a different pytest mapping:

1. **`vi.fn()` for inline mocks** (e.g., `complete: vi.fn(async () => { ... })`) — used heavily in `engine.test.ts`, `expansion-auth.test.ts`, fixture harness. **Python:** `unittest.mock.AsyncMock()` / `unittest.mock.MagicMock()`. `pytest-mock`'s `mocker.AsyncMock()` is fine too.
2. **`vi.mock("@mariozechner/pi-ai", ...)` for module-level replacement** — used in `summarize.test.ts`, `circuit-breaker.test.ts`, `extract-auth-failure.test.ts`. **Python:** `monkeypatch.setattr("path.to.module.symbol", replacement)`. For full-module substitution, `monkeypatch.setitem(sys.modules, "module_name", fake_module)` or use `unittest.mock.patch("module.function")`.
3. **`vi.spyOn(obj, "method")`** — used in 1 file (`expansion.test.ts`). **Python:** `mocker.spy(obj, "method")` or `unittest.mock.patch.object(obj, "method", wraps=obj.method)`.
4. **`vi.hoisted(() => ({...}))`** (3 files: `summarize.test.ts`, `circuit-breaker.test.ts`, `lcm-summarizer-reasoning.test.ts`) — sets up a mock object hoisted above import order. **Python:** just declare it in the test module; Python doesn't have the same import-order problem.

---

## Common fixtures

LCM's tests use four cross-cutting setup patterns. Each gets a pytest fixture in `tests/conftest.py`:

```python
# tests/conftest.py
from pathlib import Path
import sqlite3
import pytest
from hermes.db.migration import run_migrations
from hermes.db.connection import open_lcm_connection


@pytest.fixture
def db_in_memory():
    """Plain in-memory SQLite + LCM migrations. Mirrors `new DatabaseSync(':memory:')` in TS."""
    conn = sqlite3.connect(":memory:")
    run_migrations(conn, fts5_available=_fts5_available())
    yield conn
    conn.close()


@pytest.fixture
def db_with_vec0(tmp_path):
    """In-memory DB with sqlite-vec loaded. Auto-skips if vec0 not installed."""
    if not _vec0_available():
        pytest.skip("sqlite-vec extension not loadable")
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    conn.load_extension(_vec0_path())
    run_migrations(conn, fts5_available=True)
    yield conn
    conn.close()


@pytest.fixture
def fake_voyage(monkeypatch):
    """Mocks the Voyage HTTP client. Records calls; returns canned vectors."""
    calls = []
    async def fake_embed_texts(*, model, texts, input_type, api_key, **kw):
        calls.append({"model": model, "texts": texts, "input_type": input_type})
        return EmbedResponse(
            vectors=[np.zeros(1024, dtype=np.float32) for _ in texts],
            total_tokens=len(texts),
            model=model,
        )
    monkeypatch.setattr("hermes.voyage.client.embed_texts", fake_embed_texts)
    return calls  # tests can inspect


@pytest.fixture
def fake_llm(monkeypatch):
    """Replaces the LLM dispatch with a recording mock. Mirrors v41-mock-llm.ts."""
    from tests.fixtures.mock_llm import MockLlmCall
    mock = MockLlmCall(default_shape="good")
    monkeypatch.setattr("hermes.synthesis.dispatch.LlmCall", lambda *a, **kw: mock)
    return mock


@pytest.fixture
def test_corpus(db_in_memory):
    """Synthetic conversation corpus. Port of fixtures/v41-test-corpus.ts."""
    from tests.fixtures.test_corpus import build_test_corpus
    build_test_corpus(db_in_memory)
    return db_in_memory


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Replicates vitest.config.ts behavior: $HOME -> a clean tmpdir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    return tmp_path
```

**Port priority:**
1. `tmp_home` and `db_in_memory` (used by ~80% of tests)
2. `v41-mock-llm.ts` → `tests/fixtures/mock_llm.py` (the deterministic adversarial-shape mock)
3. `v41-test-corpus.ts` → `tests/fixtures/test_corpus.py` (THE_FIVE_QUESTIONS substrate)
4. `v41-stress-corpus.ts` → `tests/fixtures/stress_corpus.py`
5. `v41-tool-harness.ts` → `tests/fixtures/tool_harness.py` (`make_test_deps`, `make_test_engine`)

---

## Configuration surface — full inventory

### Source files
- **Defaults + resolver:** `/Volumes/LEXAR/Claude/lossless-claw/src/db/config.ts` (630 lines, `resolveLcmConfig(env, pluginConfig) -> LcmConfig`)
- **Schema:** `/Volumes/LEXAR/Claude/lossless-claw/openclaw.plugin.json` (504 lines — `configSchema` block + `uiHints` for operator UI)
- **State-dir resolver:** `resolveOpenclawStateDir(env)` — precedence is `OPENCLAW_STATE_DIR` → `~/.openclaw`

### Env vars (alphabetical, full list of 67 vars referenced from `src/`)
Three categories: **LCM-controlled** (overrides `LcmConfig` fields, 49 vars), **OpenClaw-host** (delivery context, 4 vars), and **integration / standard** (5 vars).

#### LCM-controlled (overrides `LcmConfig` fields)
| Var | Field overridden | Default | Notes |
|---|---|---|---|
| `LCM_ENABLED` | `enabled` | `true` | `"false"` → off |
| `LCM_DATABASE_PATH` | `databasePath` | `$OPENCLAW_STATE_DIR/lcm.db` | falls back to `~/.openclaw/lcm.db` |
| `LCM_LARGE_FILES_DIR` | `largeFilesDir` | `$OPENCLAW_STATE_DIR/lcm-files` | |
| `LCM_IGNORE_SESSION_PATTERNS` | `ignoreSessionPatterns` | `[]` | comma-separated glob list |
| `LCM_STATELESS_SESSION_PATTERNS` | `statelessSessionPatterns` | `[]` | comma-separated glob list |
| `LCM_SKIP_STATELESS_SESSIONS` | `skipStatelessSessions` | `true` | |
| `LCM_CONTEXT_THRESHOLD` | `contextThreshold` | `0.75` | 0–1 |
| `LCM_FRESH_TAIL_COUNT` | `freshTailCount` | `64` | |
| `LCM_FRESH_TAIL_MAX_TOKENS` | `freshTailMaxTokens` | (undefined) | |
| `LCM_PROMPT_AWARE_EVICTION_ENABLED` | `promptAwareEviction` | `false` | **opt-in only** (breaks pyramid invariant — see config.ts comment) |
| `LCM_NEW_SESSION_RETAIN_DEPTH` | `newSessionRetainDepth` | `2` | |
| `LCM_LEAF_MIN_FANOUT` | `leafMinFanout` | `8` | |
| `LCM_CONDENSED_MIN_FANOUT` | `condensedMinFanout` | `4` | |
| `LCM_CONDENSED_MIN_FANOUT_HARD` | `condensedMinFanoutHard` | `2` | |
| `LCM_INCREMENTAL_MAX_DEPTH` | `incrementalMaxDepth` | `1` | `-1` = unlimited |
| `LCM_LEAF_CHUNK_TOKENS` | `leafChunkTokens` | `20000` | |
| `LCM_BOOTSTRAP_MAX_TOKENS` | `bootstrapMaxTokens` | `max(6000, leafChunkTokens*0.3)` | |
| `LCM_LEAF_TARGET_TOKENS` | `leafTargetTokens` | `4000` | v4.1 raised from 2400 |
| `LCM_CONDENSED_TARGET_TOKENS` | `condensedTargetTokens` | `2000` | |
| `LCM_MAX_EXPAND_TOKENS` | `maxExpandTokens` | `4000` | |
| `LCM_LARGE_FILE_TOKEN_THRESHOLD` | `largeFileTokenThreshold` | `25000` | |
| `LCM_SUMMARY_PROVIDER` | `summaryProvider` | `""` | |
| `LCM_SUMMARY_MODEL` | `summaryModel` | `""` | |
| `LCM_LARGE_FILE_SUMMARY_PROVIDER` | `largeFileSummaryProvider` | `""` | |
| `LCM_LARGE_FILE_SUMMARY_MODEL` | `largeFileSummaryModel` | `""` | |
| `LCM_EXPANSION_PROVIDER` | `expansionProvider` | `""` | |
| `LCM_EXPANSION_MODEL` | `expansionModel` | `""` | |
| `LCM_DELEGATION_TIMEOUT_MS` | `delegationTimeoutMs` | `120000` | |
| `LCM_SUMMARY_TIMEOUT_MS` | `summaryTimeoutMs` | `60000` | |
| `LCM_PRUNE_HEARTBEAT_OK` | `pruneHeartbeatOk` | `false` | |
| `LCM_TRANSCRIPT_GC_ENABLED` | `transcriptGcEnabled` | `false` | **drops on Hermes** — transcript GC is openclaw-specific |
| `LCM_AGENT_COMPACTION_TOOL_ENABLED` | `agentCompactionToolEnabled` | `false` | operator opt-in for `lcm_compact` tool |
| `LCM_PROACTIVE_THRESHOLD_COMPACTION_MODE` | `proactiveThresholdCompactionMode` | `"deferred"` | `deferred` \| `inline` |
| `LCM_AUTO_ROTATE_SESSION_FILES_ENABLED` | `autoRotateSessionFiles.enabled` | `true` | |
| `LCM_AUTO_ROTATE_SESSION_FILES_SIZE_BYTES` | `autoRotateSessionFiles.sizeBytes` | `2097152` | 2 MB |
| `LCM_AUTO_ROTATE_SESSION_FILES_STARTUP` | `autoRotateSessionFiles.startup` | `"rotate"` | `rotate` \| `warn` \| `off` |
| `LCM_AUTO_ROTATE_SESSION_FILES_RUNTIME` | `autoRotateSessionFiles.runtime` | `"rotate"` | same set |
| `LCM_MAX_ASSEMBLY_TOKEN_BUDGET` | `maxAssemblyTokenBudget` | (undefined) | optional hard ceiling |
| `LCM_TOOL_RESULT_TOKEN_BUDGET` | `toolResultTokenBudget` | (undefined; runtime default 10000, floor 2000) | |
| `LCM_SUMMARY_MAX_OVERAGE_FACTOR` | `summaryMaxOverageFactor` | `3` | |
| `LCM_CUSTOM_INSTRUCTIONS` | `customInstructions` | `""` | |
| `LCM_CIRCUIT_BREAKER_THRESHOLD` | `circuitBreakerThreshold` | `5` | |
| `LCM_CIRCUIT_BREAKER_COOLDOWN_MS` | `circuitBreakerCooldownMs` | `1800000` | 30 min |
| `LCM_FALLBACK_PROVIDERS` | `fallbackProviders` | `[]` | csv `provider/model,provider/model` |
| `LCM_CACHE_AWARE_COMPACTION_ENABLED` | `cacheAwareCompaction.enabled` | `true` | |
| `LCM_CACHE_TTL_SECONDS` | `cacheAwareCompaction.cacheTTLSeconds` | `300` | 5 min |
| `LCM_MAX_COLD_CACHE_CATCHUP_PASSES` | `cacheAwareCompaction.maxColdCacheCatchupPasses` | `2` | |
| `LCM_HOT_CACHE_PRESSURE_FACTOR` | `cacheAwareCompaction.hotCachePressureFactor` | `4` | min 1 |
| `LCM_HOT_CACHE_BUDGET_HEADROOM_RATIO` | `cacheAwareCompaction.hotCacheBudgetHeadroomRatio` | `0.2` | clamped to `[0, 0.95]` |
| `LCM_COLD_CACHE_OBSERVATION_THRESHOLD` | `cacheAwareCompaction.coldCacheObservationThreshold` | `3` | |
| `LCM_CRITICAL_BUDGET_PRESSURE_RATIO` | `cacheAwareCompaction.criticalBudgetPressureRatio` | `0.70` | clamped to `[0, 1]` |
| `LCM_DYNAMIC_LEAF_CHUNK_TOKENS_ENABLED` | `dynamicLeafChunkTokens.enabled` | `true` | |
| `LCM_DYNAMIC_LEAF_CHUNK_TOKENS_MAX` | `dynamicLeafChunkTokens.max` | `max(leafChunkTokens, leafChunkTokens*2)` | floor = static `leafChunkTokens` |

#### Embeddings / extraction-only (not in `LcmConfig`, consumed directly)
| Var | Default | Notes |
|---|---|---|
| `LCM_DEFAULT_TOKEN_BUDGET` | (unset) | Fallback when runtime budget missing. Read in `src/engine.ts`. |
| `LCM_SQLITE_VEC_PATH` | (unset) | Explicit path to `vec0.<dylib\|so\|dll>`. Highest precedence in `candidateVec0Paths()`. |
| `LCM_DISABLE_SEMANTIC` | `false` | Bypass for the whole semantic stack (used by ops). |
| `LCM_EMBEDDING_DIM` | `1024` | Vector dim — must match the active Voyage model. |
| `LCM_EMBEDDING_MODEL` | `voyage-4-large` | |
| `LCM_EXTRACTION_LLM_ENABLED` | `false` | Entity-extractor LLM gate. |

#### Test-only env vars
| Var | Default | Notes |
|---|---|---|
| `LCM_TEST_VEC0_PATH` | (auto-discover) | Tests gate `describe.skipIf(!VEC0_AVAILABLE)` on `existsSync(this)`. Eva's box uses `/Users/lume/.openclaw/extensions/node_modules/sqlite-vec-darwin-arm64/vec0.dylib`. |
| `REAL_HOME` | `/Users/lume` | Fallback for tests that override `HOME`. |
| `HOME` | (real $HOME) | vitest config rewrites this to a tmpdir per run. |
| `ANTHROPIC_API_KEY` | (unset) | Used by tests that hit the real Anthropic API (rare; mostly mocked). |

#### Host-supplied (delivered by OpenClaw, not LCM)
| Var | Purpose | Notes |
|---|---|---|
| `OPENCLAW_STATE_DIR` | Profile state dir | Resolves to `~/.openclaw` if unset. **Hermes:** rename to `HERMES_HOME`, default `~/.hermes`. |
| `OPENCLAW_PROVIDER` | Provider name override | Used in 1 place in src — for runtime model resolution. |
| `OPENCLAW_AGENT_DIR` | Agent file dir | Read from src. |
| `PI_CODING_AGENT_DIR` | pi-coding-agent override | Read from src. |

#### Integration / standard env
| Var | Notes |
|---|---|
| `VOYAGE_API_KEY` | Required for live embeddings + live integration tests. Mocked everywhere in unit tests. |
| `TZ` | IANA timezone for summary timestamps. Falls back to `Intl.DateTimeFormat().resolvedOptions().timeZone`. |

### Hermes-side env-var translation
| Original (OpenClaw / LCM) | Hermes | Notes |
|---|---|---|
| `OPENCLAW_STATE_DIR` | `HERMES_HOME` | Default `~/.hermes`. |
| `LCM_DATABASE_PATH` | `HERMES_DATABASE_PATH` *(or keep `LCM_*` for transition)* | Suggest renaming all `LCM_*` → `HERMES_LCM_*` once stable, but keep `LCM_*` as deprecated aliases for v0.x. |
| `LCM_LARGE_FILES_DIR` | `HERMES_LARGE_FILES_DIR` | |
| `LCM_TRANSCRIPT_GC_ENABLED` | **DROP** | Transcript GC was OpenClaw-specific (rewrites session JSONL); Hermes is the host so this is N/A. Keep the config field but document it as no-op for back-compat. |
| `LCM_AGENT_COMPACTION_TOOL_ENABLED` | `HERMES_AGENT_COMPACTION_TOOL_ENABLED` | The `lcm_compact` tool itself is in scope; just renamed. |
| `LCM_AUTO_ROTATE_SESSION_FILES_*` | likely **DROP** | These rotate OpenClaw's session JSONL files. Hermes owns its own session storage — pick its rotation policy independently. |
| `LCM_SQLITE_VEC_PATH` | `HERMES_SQLITE_VEC_PATH` | |
| `LCM_TEST_VEC0_PATH` | `HERMES_TEST_VEC0_PATH` | |
| `VOYAGE_API_KEY` | **keep** | Standard 3rd-party var; don't rename. |
| `TZ` | **keep** | POSIX standard. |
| `OPENCLAW_PROVIDER`, `OPENCLAW_AGENT_DIR`, `PI_CODING_AGENT_DIR` | **DROP** | These are pi-agent / OpenClaw internals. Hermes has its own provider-resolution path. |

### Config fields (from `LcmConfig` type — full inventory)
Source of truth is `src/db/config.ts:82-190`. Schema validation lives in `openclaw.plugin.json:248-502`.

| Field | Type | Default | Schema constraints | Notes |
|---|---|---|---|---|
| `enabled` | bool | `true` | — | |
| `databasePath` | string | `$STATE_DIR/lcm.db` | — | aka legacy alias `dbPath` |
| `largeFilesDir` | string | `$STATE_DIR/lcm-files` | — | |
| `ignoreSessionPatterns` | string[] | `[]` | items: string | glob patterns |
| `statelessSessionPatterns` | string[] | `[]` | items: string | |
| `skipStatelessSessions` | bool | `true` | — | |
| `contextThreshold` | float | `0.75` | min 0, max 1 | compaction trigger fraction |
| `freshTailCount` | int | `64` | min 1 | |
| `freshTailMaxTokens` | int? | (undefined) | min 0 | optional token cap for fresh tail |
| `promptAwareEviction` | bool | `false` | — | **invariant-breaking opt-in** |
| `newSessionRetainDepth` | int | `2` | min -1 | -1 = keep all |
| `leafMinFanout` | int | `8` | min 2 | |
| `condensedMinFanout` | int | `4` | min 2 | |
| `condensedMinFanoutHard` | int | `2` | min 2 | |
| `incrementalMaxDepth` | int | `1` | min -1 | -1 = unlimited |
| `leafChunkTokens` | int | `20000` | min 1 | |
| `bootstrapMaxTokens` | int? | `max(6000, leafChunkTokens*0.3)` | min 1 | |
| `leafTargetTokens` | int | `4000` | min 1 | v4.1: was 2400 |
| `condensedTargetTokens` | int | `2000` | min 1 | |
| `maxExpandTokens` | int | `4000` | min 1 | |
| `largeFileTokenThreshold` | int | `25000` | min 1000 | aka `largeFileThresholdTokens` |
| `summaryProvider` | string | `""` | — | |
| `summaryModel` | string | `""` | — | |
| `largeFileSummaryProvider` | string | `""` | — | |
| `largeFileSummaryModel` | string | `""` | — | |
| `expansionProvider` | string | `""` | — | |
| `expansionModel` | string | `""` | — | |
| `delegationTimeoutMs` | int | `120000` | min 1 | |
| `summaryTimeoutMs` | int | `60000` | min 1 | |
| `timezone` | string | system TZ | — | |
| `pruneHeartbeatOk` | bool | `false` | — | |
| `transcriptGcEnabled` | bool | `false` | — | **DROP on Hermes** |
| `agentCompactionToolEnabled` | bool | `false` | — | |
| `proactiveThresholdCompactionMode` | enum | `"deferred"` | enum: `["deferred","inline"]` | |
| `autoRotateSessionFiles.enabled` | bool | `true` | — | likely DROP on Hermes |
| `autoRotateSessionFiles.sizeBytes` | int | `2097152` | min 1 | |
| `autoRotateSessionFiles.startup` | enum | `"rotate"` | enum: `["rotate","warn","off"]` | |
| `autoRotateSessionFiles.runtime` | enum | `"rotate"` | enum: `["rotate","warn","off"]` | |
| `maxAssemblyTokenBudget` | int? | (undefined) | min 1000 | hard ceiling |
| `toolResultTokenBudget` | int? | (undefined; runtime default 10000) | min 2000 | per-tool result cap |
| `summaryMaxOverageFactor` | float | `3` | min 1 | |
| `customInstructions` | string | `""` | — | injected into all summary prompts |
| `circuitBreakerThreshold` | int | `5` | min 1 | |
| `circuitBreakerCooldownMs` | int | `1800000` | min 1 | 30 min |
| `fallbackProviders` | `[{provider,model}]` | `[]` | items: `{required:[provider,model]}` | |
| `cacheAwareCompaction.enabled` | bool | `true` | — | |
| `cacheAwareCompaction.cacheTTLSeconds` | int | `300` | min 1 | |
| `cacheAwareCompaction.maxColdCacheCatchupPasses` | int | `2` | min 1 | |
| `cacheAwareCompaction.hotCachePressureFactor` | float | `4` | min 1 | |
| `cacheAwareCompaction.hotCacheBudgetHeadroomRatio` | float | `0.2` | min 0, max 0.95 | |
| `cacheAwareCompaction.coldCacheObservationThreshold` | int | `3` | min 1 | |
| `cacheAwareCompaction.criticalBudgetPressureRatio` | float? | `0.70` | min 0, max 1 | exported as `DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO` |
| `dynamicLeafChunkTokens.enabled` | bool | `true` | — | |
| `dynamicLeafChunkTokens.max` | int | derived (≥ `leafChunkTokens`) | min 1 | floor is the static `leafChunkTokens` |

### Exported constants worth preserving
- `DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO = 0.70` — referenced by tests + runtime + resolver fallback. Mirror in Python as `hermes.config.DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO`.
- `DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES = 2 * 1024 * 1024` — same single-source-of-truth pattern.

### Precedence rules (the key contract `config.test.ts` enforces)
The 61-test `config.test.ts` pins this exact precedence — port it as Hermes's `tests/test_config.py`:
1. **Env var** (string-parsed; non-finite values fall through, do not crash)
2. **Plugin config field** (typed-coerced; unknown keys silently ignored if `additionalProperties: false`)
3. **Hardcoded default** (the `?? 0.75` style fallback at the end of every chain)

For pattern arrays (`ignoreSessionPatterns`, `statelessSessionPatterns`), the resolver also returns a **diagnostics** record tracking whether env overrode plugin-config — Hermes needs the same so the `lcm doctor` equivalent can show operators where each value came from.

### Hermes config delivery
LCM was a plugin, so `pluginConfig` came from OpenClaw's plugin-config registry. Hermes is its own host — pick one of:

**Option A — YAML config file (recommended):**
```yaml
# ~/.hermes/config.yaml
context:
  engine: lcm
  lcm:
    enabled: true
    context_threshold: 0.75
    leaf_chunk_tokens: 20000
    fresh_tail_count: 64
    summary_provider: anthropic
    summary_model: claude-haiku-4-5
    voyage_api_key: "${VOYAGE_API_KEY}"     # env interpolation
    embedding_model: voyage-4-large
    cache_aware_compaction:
      enabled: true
      cache_ttl_seconds: 300
      critical_budget_pressure_ratio: 0.70
    dynamic_leaf_chunk_tokens:
      enabled: true
      max: 40000
    fallback_providers:
      - {provider: openai-codex, model: gpt-5.4-mini}
      - {provider: anthropic, model: claude-haiku-4-5}
```

Use `pydantic-settings` for parsing + env override; `pydantic.Field(default=...)` mirrors `??` fallback exactly. Reject unknown keys with `model_config = ConfigDict(extra="forbid")` (mirrors `additionalProperties: false`).

**Option B — TOML** (`~/.hermes/config.toml`): same shape, less env-interpolation grace; works if you'd rather avoid YAML dependency.

**Option C — env-only** (no config file): every field has an env override anyway; Hermes could ship without a config file initially and add YAML in 0.2. Costs operators a `set -a; source ~/.hermes.env; set +a` ritual.

### Config diagnostics — port `resolveLcmConfigWithDiagnostics`
LCM's `resolveLcmConfigWithDiagnostics` returns `(config, diagnostics)` where diagnostics records pattern-array provenance (env / plugin-config / default) and whether env overrode plugin-config. Port as:
```python
class LcmConfigDiagnostics(BaseModel):
    ignore_session_patterns_source: Literal["env", "config", "default"]
    stateless_session_patterns_source: Literal["env", "config", "default"]
    ignore_session_patterns_env_overrides_config: bool
    stateless_session_patterns_env_overrides_config: bool

def resolve_lcm_config_with_diagnostics(env: Mapping[str, str], config_dict: Mapping[str, Any]) -> tuple[LcmConfig, LcmConfigDiagnostics]:
    ...
```
`hermes doctor` (the equivalent of `lcm-doctor`) consumes diagnostics to render "compiled from ENV vs config file vs default".

---

## Test database fixtures

LCM's fixtures are all **code-built in-memory DBs**, not checked-in `.db` files:

```
test/fixtures/
  v41-test-corpus.ts      → buildTestCorpus(db: DatabaseSync) seeds a known synthetic conversation
  v41-stress-corpus.ts    → larger corpus for v41-stress-fixture.test.ts
  v41-tool-harness.ts     → makeTestDeps() + makeTestEngine() — shared mock factories
  v41-mock-llm.ts         → deterministic mock LlmCall with adversarial response shapes
```

There are **no binary fixture DBs**, no `.sqlite` files in the test tree, and no snapshot files. This makes the port easier — just translate the seed functions to Python (same `INSERT` statements against `sqlite3.Connection`).

Outside `test/fixtures/`, the `scripts/v41-qa-runner.mjs` script runs the same 25 THE_FIVE_QUESTIONS scenarios against a snapshot of Eva's real `~/.openclaw/lcm.db` (2.6 GB) — that's a manual smoke test, not part of the CI suite. Port the script to `scripts/hermes_qa_runner.py` once the test suite is green.

---

## CI configuration

### Source: `.github/workflows/ci.yml`
LCM's CI is minimal — 2 jobs:
1. **test** — Node 22, `npm ci`, `npm test` (vitest)
2. **smoke-latest-openclaw** — build the plugin bundle, install against `openclaw@latest`, verify the bundle's `default.register` surface exists. Catches ESM-resolution drift.

Both run on `ubuntu-latest` only.

### Recommended Hermes CI
```yaml
# .github/workflows/ci.yml
name: CI
on: [push, pull_request]
jobs:
  test:
    strategy:
      matrix:
        python: ["3.11", "3.12"]
        os: [ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ matrix.python }} }
      - run: pip install -e ".[dev]"
      - run: ruff check .
      - run: mypy hermes/
      - run: pytest -v --cov=hermes --cov-report=xml
      - uses: codecov/codecov-action@v4
        with: { files: ./coverage.xml }
  live-voyage:
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    env:
      VOYAGE_API_KEY: ${{ secrets.VOYAGE_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev]"
      - run: pytest -m live
```

**Rationale for the matrix:**
- 3.11 + 3.12 catches Python version drift; Hermes's target floor is 3.11 (matches modern `pathlib`, `tomllib`, asyncio refinements)
- ubuntu + macOS catches sqlite-vec dylib/so packaging issues; OpenClaw ships per-platform `sqlite-vec-darwin-arm64` / `sqlite-vec-linux-x64` packages and the test gating logic depends on the right one being present
- Live-Voyage as separate job (PR jobs skip it; main pushes run it on a secret) — protects the 0.20¢-per-run cost from PR spam

### Tooling stack
| Concern | LCM (TS) | Hermes (Python) |
|---|---|---|
| Test runner | vitest 3.x | pytest 8.x |
| Test discovery | `**/*.test.ts` | `tests/**/test_*.py` (or `test_*.py` anywhere; configure in `pyproject.toml`) |
| Async tests | native | `pytest-asyncio` (set `asyncio_mode = "auto"` in `pyproject.toml`) |
| Mocking | built-in `vi` | `pytest-mock` (wraps `unittest.mock`) + `monkeypatch` |
| Lint | (none configured) | `ruff` |
| Type check | `tsc --noEmit` (build only) | `mypy hermes/` |
| Coverage | (not run) | `pytest-cov` (target: 80% line, mirror LCM's apparent ~85% logical coverage) |
| Bundle smoke | esbuild + register check | `python -c "import hermes; hermes.boot()"` (smoke import) |

---

## Open decisions / ADRs to write
- **ADR — Test framework:** pytest. Obvious default, no real alternative.
- **ADR — Coverage target:** 80% line coverage, no per-module gates. LCM doesn't measure but logical coverage is ~85% based on subsystem-by-subsystem inspection; matching that is a stretch goal not a release gate.
- **ADR — Snapshot testing:** LCM doesn't use any (verified: 0 hits for `toMatchSnapshot` / `toMatchInlineSnapshot`). Hermes shouldn't introduce snapshot tests during the port — they'd be 1.0 features.
- **ADR — Live integration tests:** Gated behind `pytest.mark.live` + opt-in. Two suites: `live_voyage` (needs `VOYAGE_API_KEY`) and `live_llm` (needs Anthropic/provider key). Default `pytest` skips both.
- **ADR — Env-var rename policy:** Phase 1 keeps `LCM_*` env vars accepted as aliases (deprecation warning, removed in 0.2). Phase 2 promotes `HERMES_*` as primary. Phase 3 drops `LCM_*`.
- **ADR — Config delivery format:** YAML via `pydantic-settings` (recommend) vs TOML vs env-only.
- **ADR — Config schema validation:** Mirror the JSON-schema constraints from `openclaw.plugin.json` (mins, maxes, enums, `additionalProperties: false`) as `pydantic.Field` validators. The `additionalProperties: false` behavior is critical — drift-detector tests in LCM rely on it; mirror it via `model_config = ConfigDict(extra="forbid")`.

---

## Remaining 5% risk
<unknowns>
1. **Async-mock semantics drift** — `unittest.mock.AsyncMock` has subtle behavior differences from `vi.fn(async () => ...)` around `await` vs sync return. Tests that chain awaits on mocked methods may need cleanup pass during port.
2. **SQLite extension loading on Python** — `python -m sqlite3` doesn't enable `enable_load_extension` by default on macOS Homebrew Python builds. Hermes must either ship its own sqlite3 build (vendored binary or via `pysqlite3-binary`) or document the install ritual.
3. **vec0 path-discovery** — LCM's TS `candidateVec0Paths()` hand-rolls a 4-entry probe list across `LCM_SQLITE_VEC_PATH`, plugin-local `node_modules`, `~/.openclaw/extensions`, and a `homedir()` fallback. Python must port the equivalent ordering but adapt for `site-packages` vs `node_modules`.
4. **vitest's `vi.hoisted()` import-order trick** — used in 3 files (`summarize.test.ts`, `circuit-breaker.test.ts`, `lcm-summarizer-reasoning.test.ts`). Python doesn't have this hoisting problem, but tests that *relied* on the hoist (mocking before import) need careful translation — usually they collapse to a plain `monkeypatch.setattr` in a fixture.
5. **`describe.skipIf` granularity** — vitest skips at the `describe` block (a whole block of tests). pytest's `pytest.mark.skipif` works per-test or via `pytestmark` at module/class scope. Tests like `embeddings-store.test.ts` (mixed always-run + vec0-gated `describe.skipIf`) need careful class-level skipping in pytest to preserve the gating semantics.
6. **THE_FIVE_QUESTIONS predicate language** — `v41-five-questions.test.ts` uses Python-style `predicate: (response) => string | null` returning either null (PASS) or an error string (FAIL). The shape is a soft contract referenced from `scripts/v41-qa-runner.mjs`. Port should preserve this — Python pytest's `assert` won't quite match because predicate-based assertions give human-readable failure messages that the bare assert form swallows. Consider keeping a tiny predicate helper that mirrors the contract.
7. **Test counts likely shift** — pytest `parametrize` consolidates table-driven cases that vitest spells as multiple `it(...)` blocks. Don't be alarmed if Python totals are 10–20% lower while logical coverage is unchanged. Conversely, `expect(...).rejects.toThrow()` chains often split into 2 cases in Python (one for the call, one for the error type), pushing the total back up.
8. **Coverage parity is hard to measure** — vitest doesn't run coverage on LCM's main branch, so there's no concrete baseline to match. Pick "80% line, no per-module gates" as the rallying target; revisit once the port is done and the Python suite has a baseline.
9. **`autoRotateSessionFiles` + `transcriptGcEnabled` semantics** — both are OpenClaw-specific behaviors. Keeping them in `LcmConfig` as inert fields preserves the schema shape; dropping them simplifies the surface. Recommend keeping for v0.1 (back-compat for users migrating LCM-format configs); dropping for v1.0.
10. **Plugin contract surface (`openclaw.plugin.json`)** — Hermes doesn't have a plugin manifest. The 8 tool names from `contracts.tools` (`lcm_grep`, `lcm_describe`, ...) become Hermes's MCP tool registrations; the manifest's `kind: "context-engine"` activation marker becomes the entry-point registration in Hermes's MCP server.
</unknowns>
