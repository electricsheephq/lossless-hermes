---
name: Port issue
about: Port Eva's 31-query stratified eval fixture (eva-baseline-v2)
title: '[epic-09] eval: port eva-baseline-v2 31-query fixture'
labels: 'port, epic-09-eval, fixture'
---

## Source (TypeScript)

- **No single TS file holds the fixture content.** The 31-query corpus is referenced by name (`eva-baseline-v2`) across:
  - `src/eval/query-set.ts:54` (docstring example)
  - `test/eval-query-set.test.ts:44,48,49,78,111,122` (fixture name used in identity tests)
  - `src/embeddings/hybrid-search.ts:8,319` (the "+52.5pp on Eva's 31-query eval" comment)
  - `test/fixtures/v41-test-corpus.ts:273` (mentions the spike result; doesn't carry the queries themselves)
  - `docs/v4.1/PR_DESCRIPTION.md:325-334` (the per-stratum result table: 14 fts-easy / 9 fts-medium / 8 paraphrastic)
- The actual query texts + ground-truth `expected_summary_ids` lived in **Eva's local LCM DB snapshot** when she ran the Phase A spike. They were never committed upstream because they encode private content from her real conversation corpus.

## Target (Python)

- File: `tests/fixtures/eva_baseline_v2.py`
- Estimated LOC: ~250 (31 query records × ~6 lines each + builder + provenance docstring)

## What this issue covers

Stand up a checked-in 31-query stratified eval fixture that:

1. **Matches the published stratum distribution exactly**: 14 fts-easy + 9 fts-medium + 8 paraphrastic = 31 queries.
2. **Resolves the provenance gap.** Two paths; pick one and document the choice in the fixture's module docstring:
   - **Path A — Recover Eva's fixture (preferred if accessible).** Read `eva-baseline-v2` rows from Eva's `~/.openclaw/lcm.db` snapshot (or a sanitized export she provides), re-export to `eva_baseline_v2.py` as Python literals, redact PII at the boundary. This produces a fixture that's byte-comparable to TS-baseline runs.
   - **Path B — Rebuild from `v41-test-corpus.ts` (fallback).** Author a 31-query set against the synthetic corpus that `tests/fixtures/test_corpus.py` (Epic 01-08 port of `v41-test-corpus.ts`) seeds. Picks summary IDs that exist in that corpus; the stratum tags follow the same definitions: fts-easy = exact term match in target summary; fts-medium = single-token paraphrase or coreference; paraphrastic = semantic-only (no surface overlap).
3. **Ships a deterministic builder** — `build_eva_baseline_v2() -> list[QueryRecord]` returning the canonical list in `query_id` order. The fixture file is the source of truth; tests call the builder.
4. **Registers idempotently** — pytest fixture `eva_baseline_v2_registered(db_in_memory)` calls `register_query_set(db, {"name": "eva-baseline", "version": 2}, build_eva_baseline_v2())` once per session.

## Stratum definitions (verbatim from TS source comments)

| Stratum | Definition | Eva's n |
|---|---|---:|
| `fts-easy` | Query terms appear verbatim in at least one expected summary. FTS5 alone should find them. | 14 |
| `fts-medium` | Single-token paraphrase or coreference (e.g., "she" → person name; "the file" → filename). FTS5 with stemming finds most; semantic lifts the rest. | 9 |
| `paraphrastic` | No surface overlap. Pure semantic ("how did we decide X?" when the conversation never used the word X). FTS5 baseline ~5%; rerank is the differentiator. | 8 |

## Python public API

```python
from lossless_hermes.eval.query_set import QueryRecord, QuerySetIdentity, register_query_set

EVA_BASELINE_V2_IDENTITY = QuerySetIdentity(name="eva-baseline", version=2)

def build_eva_baseline_v2() -> list[QueryRecord]:
    """31 queries — 14 fts-easy + 9 fts-medium + 8 paraphrastic.

    Provenance: see module docstring (Path A or Path B; one is chosen at port time).
    """
    return [
        QueryRecord(
            query_id="eva-fe-001",
            query_text="...",
            stratum="fts-easy",
            expected_summary_ids=["sum-..."],
            reference_summary="... (optional)",
        ),
        # ... 30 more ...
    ]

# pytest fixture for tests that need the seeded corpus
@pytest.fixture
def eva_baseline_v2_registered(db_in_memory) -> sqlite3.Connection:
    register_query_set(db_in_memory, EVA_BASELINE_V2_IDENTITY, build_eva_baseline_v2())
    return db_in_memory
```

`query_id` convention: `eva-<stratum-initials>-NNN` (e.g., `eva-fe-001` for fts-easy #1, `eva-fm-001` for fts-medium, `eva-p-001` for paraphrastic). Stable IDs survive fixture edits.

## Dependencies

- **Depends on:** #09-01 (`QueryRecord`, `register_query_set`).
- **Blocks:** #09-07 (the CI live-eval workflow runs against this fixture), #09-08 (the +52.5pp benchmark **runs on this fixture**; no fixture, no benchmark).

## Acceptance criteria

- [ ] `build_eva_baseline_v2()` returns exactly 31 `QueryRecord` instances.
- [ ] Stratum counts: 14 fts-easy + 9 fts-medium + 8 paraphrastic (assert via `Counter([q.stratum for q in build_eva_baseline_v2()])`).
- [ ] `query_id`s are unique within the set.
- [ ] Every query has non-empty `query_text` and a valid stratum.
- [ ] At least the 8 paraphrastic queries carry `expected_summary_ids` (paraphrastic is the +52.5pp stratum; without ground-truth there's nothing to measure).
- [ ] Module docstring explicitly states which path (A or B) was taken AND the date of the snapshot/corpus the IDs were resolved against. Future maintainers can re-verify the ground-truth.
- [ ] `eva_baseline_v2_registered` pytest fixture round-trips: `get_query_set(db, EVA_BASELINE_V2_IDENTITY)` returns exactly what `build_eva_baseline_v2()` produced.
- [ ] **PII audit:** if Path A was taken, fixture text is reviewed for emails, real names, SSN-like patterns, or external company names before commit. Each redacted span is replaced with `[REDACTED]` and the docstring lists the categories redacted (not the originals).
- [ ] `pytest tests/eval/test_eva_baseline_v2_fixture.py` passes (the fixture's own sanity tests: count, strata, ID uniqueness, round-trip).
- [ ] PR description states which path was chosen and any deviation from the upstream 14/9/8 split (with rationale).

## Tests

`tests/eval/test_eva_baseline_v2_fixture.py`:

- Count is exactly 31.
- Stratum distribution: 14/9/8.
- All `query_id`s unique.
- All `query_text` non-empty.
- All paraphrastic queries have ≥1 `expected_summary_id`.
- Roundtrip: register → get → compare equal.
- No PII pattern matches in `query_text` (regex-scan for email, SSN, common phone formats — defense in depth).

## Estimated effort

**5–8 hours.**

Breakdown: 2 h to negotiate which path with Eva (if Path A) or 4 h to author 31 queries fresh (if Path B), 2 h to register + write the sanity-test file, 1 h PII sweep + docstring.

## Confidence

**80%.** Lowest-confidence issue in the epic. Three risks:

1. **Path A may not be available.** Eva's snapshot DB is ~2.6 GB and contains private content; she may not want to share or sanitize it. Mitigation: Path B is always available; the +52.5pp number on Path B may not exactly match upstream's +52.5pp (we'd report whatever Python+Voyage actually measures).
2. **Stratum classification is subjective.** "fts-medium" vs "paraphrastic" can be ambiguous for borderline queries. Mitigation: encode the classification rationale in a per-query comment so a reviewer can audit.
3. **Ground-truth `expected_summary_ids` lock the fixture to a specific corpus.** If the corpus drifts (Epic 01-08's `v41-test-corpus.ts` port changes IDs), the fixture's IDs need updating. Mitigation: a separate test asserts that every `expected_summary_id` resolves to an existing row when the test corpus is seeded — fails fast on corpus drift.
