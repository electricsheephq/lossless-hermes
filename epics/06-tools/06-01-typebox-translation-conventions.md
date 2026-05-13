---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: document TypeBox → Python dict translation conventions'
labels: 'port, docs'
---

## Source (TypeScript)
- File: `src/tools/*.ts` — every tool factory uses TypeBox (`Type.String({description, enum})`, `Type.Number({minimum, maximum})`, `Type.Object({properties, required})`, `Type.Optional(...)`, `Type.Array(Type.X, {description})`).
- Lines: ~250 LOC of schema definitions across the 8 factories.
- Function(s)/class(es): the `parameters` field of every `createXxxTool` factory return value (e.g. `lcm-grep-tool.ts:43–125`, `lcm-describe-tool.ts:61–116`, etc.).

## Target (Python)
- File: `docs/porting-guides/tools.md` already contains the canonical translation table (lines 708–721) and all 8 schemas in Python-dict form. This issue's deliverable is a contributing-conventions section + a CI test that pins the translation.
- Estimated LOC: ~30 (conventions doc anchor) + ~50 (CI test) = ~80 LOC.

## Dependencies
- Depends on: ADR-016 (TypeBox → JSON Schema translation approach — already accepted).
- Blocks: #06-02 (dispatch table needs the schemas to exist), every per-tool port issue (06-07 through 06-14).

## Acceptance criteria
- [ ] One-page conventions section authored as part of `tools/__init__.py` module docstring OR as a `tools/SCHEMAS.md` companion doc, referencing the table in tools.md.
- [ ] Every `LCM_<TOOL>_SCHEMA` dict in the port carries a `# Verbatim from src/tools/<tool>.ts:<line range>` comment at the top of the dict so future readers find the TS source.
- [ ] CI test `tests/tools/test_schemas_wellformed.py` loads each schema and runs `jsonschema.Draft7Validator.check_schema(s)` — must pass for every schema in `get_tool_schemas()`.
- [ ] CI test `tests/tools/test_schemas_match_ts.py` compares the loaded Python schemas to the committed JSON-Schema export at `tests/fixtures/lcm_v4.1_schemas.json` (the export is generated once via `tsc + node` in the LCM repo and committed; ADR-016 names the fixture).
- [ ] PR description cites ADR-016 + the LCM commit SHA being ported.

## Tests
- `tests/tools/test_schemas_wellformed.py` — 1 test per schema (8 total).
- `tests/tools/test_schemas_match_ts.py` — 1 test per schema, asserts structural equality (ignoring whitespace in descriptions if and only if the fixture-export normalizes them; ADR-016 leaves the policy open — prefer byte-identical).

## Estimated effort
**2 hours** — conventions doc + CI test + fixture loader.

## Confidence
**95%** — translation table is already documented and pinned by ADR-016. The 5% risk: the LCM-side fixture-export tooling doesn't exist yet (someone has to write the `tsc + node` step that emits `lcm_v4.1_schemas.json`). Can be deferred to a follow-up if Wave A ships before the fixture exists; in that case the schema match test is `xfail` until the fixture lands.

## References
- [ADR-016](../../docs/adr/016-typebox-translation.md)
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) lines 708–721.
