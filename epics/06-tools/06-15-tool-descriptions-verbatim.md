---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: verify every tool description string is verbatim from TS'
labels: 'port, test, lint'
---

## Source (TypeScript)
- File: `src/tools/lcm-grep-tool.ts:196–204`, `src/tools/lcm-describe-tool.ts:146–155`, `src/tools/lcm-expand-tool.ts:134–142`, `src/tools/lcm-synthesize-around-tool.ts:641–653`, `src/tools/lcm-get-entity-tool.ts:123–136`, `src/tools/lcm-search-entities-tool.ts:137–150`, `src/tools/lcm-compact-tool.ts:233–240`.
- Lines: 7 description strings × ~10 lines each = ~70 LOC of model-facing prose.
- Function(s)/class(es): the `description` field of each tool factory's exported schema.

## Target (Python)
- File: `tests/tools/test_tool_descriptions_verbatim.py` — pytest module that loads the committed fixture and asserts each tool's description string is byte-identical.
- Also touches: `tests/fixtures/lcm_v4.1_tool_descriptions.json` (the committed fixture, exported once from the LCM TS source).
- Estimated LOC: ~120 LOC (test + fixture loader; the fixture itself is the descriptions verbatim, ~70 lines of prose serialized as JSON).

## Dependencies
- Depends on: every per-tool issue (06-07 through 06-14) — needs all 7 schemas registered.
- Blocks: shipping v0.1.0 — this is a release-readiness gate.

## Rationale

Description strings are load-bearing prose for the agent — the model reads them to decide which tool to call. The Wave-1 → Wave-12 audit history tuned these strings for:

- **PRIMARY-vs-secondary routing hints** (e.g. "PRIMARY for Type B topic-anchored queries", "use `lcm_get_entity` for canonical name lookup").
- **Concrete fallback suggestions** when a tool returns empty (3 specific alternative-tool calls).
- **Operator-tunable env-var names** (`LCM_TOOL_RESULT_TOKEN_BUDGET`, etc.) so operators can self-serve.
- **Parameter caps** (`hard-capped at 20 rows`, `max 4 weeks`) so the model doesn't waste tool calls.
- **Wave-12 fix annotations** (e.g. "the audit table records the resolved model that actually ran") — these are model-facing because the model uses them to interpret partial-success responses.

A converter or paraphrasing pass silently destroys this. ADR-016 codifies hand-translation; this test pins the result.

## Acceptance criteria
- [ ] **Fixture committed** at `tests/fixtures/lcm_v4.1_tool_descriptions.json`:
  ```json
  {
    "_provenance": "lossless-claw@<commit-sha>",
    "_extracted_from": "src/tools/<tool>.ts:<line range>",
    "lcm_grep": "<full verbatim description string>",
    "lcm_describe": "<...>",
    "lcm_expand": "<...>",
    "lcm_synthesize_around": "<...>",
    "lcm_get_entity": "<...>",
    "lcm_search_entities": "<...>",
    "lcm_compact": "<...>"
  }
  ```
  - **Note:** `lcm_expand_query` is in the fixture for completeness but the v0.1.0 test skips its assertion (it's not registered per [ADR-012](../../docs/adr/012-subagent-defer.md)).
- [ ] Test `test_every_registered_tool_description_matches_fixture`:
  - Loads `LCMEngine.get_tool_schemas()`.
  - For each schema, asserts `schema["description"] == fixture[schema["name"]]` byte-identical.
  - Fail message includes the diff between expected and actual (use `difflib.unified_diff`).
- [ ] Test `test_no_extra_tools_registered`:
  - Asserts the set of registered tool names is exactly `{lcm_grep, lcm_describe, lcm_expand, lcm_synthesize_around, lcm_get_entity, lcm_search_entities, lcm_compact}` in v0.1.0 (per ADR-012, no `lcm_expand_query`).
- [ ] Test `test_no_missing_tools_registered`:
  - Asserts all 7 expected tools are present.
- [ ] Test `test_fixture_provenance_matches_source_pin`:
  - Asserts `_provenance` in the fixture matches the pinned LCM commit SHA in `docs/reference/lcm-source-map.md` (so when LCM is bumped, the fixture must be regenerated and re-pinned in the same PR).
- [ ] PR description cites ADR-016 + the LCM commit SHA the fixture was extracted from.

## How to regenerate the fixture
- One-time tooling (in the LCM repo): write a small script that imports each `createXxxTool` factory, calls it with stub deps, extracts `tool.description` from the returned `AnyAgentTool`, emits JSON.
- Run it in the LCM repo at the target commit; commit the resulting JSON to `lossless-hermes/tests/fixtures/`.
- Re-run on every LCM source-pin bump.

## Tests
- The 4 tests above.

## Estimated effort
**3 hours** — 1h fixture extraction script (small), 1h test code, 1h diff-output polish so failure messages are readable.

## Confidence
**95%** — mechanical. 5% risk: the fixture-extraction script in the LCM repo is new tooling that doesn't exist yet. If it's not built before v0.1.0 ships, the fixture can be transcribed manually (it's only 7 strings × ~10 lines each — ~30 minutes by hand) and pinned with a `_provenance` marker pointing at the source files and commit.

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) line 33: "every `description` string below is **verbatim from the TS source** — these are load-bearing for the model."
- [ADR-016](../../docs/adr/016-typebox-translation.md) — hand-translation policy + fixture-comparison test pattern.
- Wave-12 N3 retro — model-facing prose earns its keep because the model reads it.
