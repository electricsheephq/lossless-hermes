# ADR-016: TypeBox → JSON schema translation approach

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

The TypeScript source declares every agent tool schema via TypeBox (`Type.Object({...})`, `Type.String({description, enum})`, etc.). The Python port must expose equivalent JSON Schema dicts on `LCMEngine.get_tool_schemas()` so Hermes can advertise tools to providers. There are ~6,500 LOC worth of tool surface (`src/tools/*.ts`), with eight tool factories whose schemas each carry hand-authored description prose (often 200–800 chars per field) that is load-bearing for the model's tool-selection behavior.

The question: how do we get from TypeBox in TS to dicts in Python — hand-translate, or auto-generate via a converter?

Constraints:
- TypeBox `Type.Strict()` already emits standards-compliant JSON Schema; the wire format isn't the problem.
- Description strings have been tuned across Wave-1 → Wave-12 audits (PRIMARY-vs-secondary routing hints, fallback suggestions, env-knob names, parameter caps). Paraphrasing them silently degrades model behavior.
- The schemas live in `tools.md` verbatim — we already have the dicts written down.

## Options considered

### Option A: Hand-translate every schema into Python dicts in `tools/*.py`

- Description: one `LCM_<TOOL>_SCHEMA` dict per tool file, copied directly from `tools.md`. Description strings pasted verbatim. Enum literals listed inline. `required` array filled by hand. The `LCMEngine.get_tool_schemas()` method just returns the list.
- Pros:
  - Description strings stay byte-identical to TS source — no risk of an automated converter normalizing whitespace or re-escaping quotes.
  - One-time work (~1 hour for all 8 schemas per the tools.md estimate). No long-term maintenance burden — these schemas change rarely, and when they do, we update both sides deliberately.
  - Zero runtime dependency on a TypeBox compatibility shim.
  - Aligns with the surrounding code: handlers, dispatch tables, and tests are already hand-ported.
- Cons:
  - Manual transcription has a typo failure mode. Mitigated by a CI test that loads each schema with `jsonschema.Draft7Validator.check_schema` and asserts well-formedness.
  - If a TS schema gets updated (new field, new enum value), the Python port can drift silently until somebody notices. Mitigated by treating tool-schema changes as deliberate cross-language ports, gated on a PR checklist.
- Evidence cited: `tools.md` already contains all 8 schemas in Python-dict form (see `LCM_GREP_SCHEMA`, `LCM_DESCRIBE_SCHEMA`, etc.). The TypeBox→JSON-Schema translation table at `tools.md:708-721` is the mapping reference.

### Option B: Auto-generate Python dicts from TypeBox at build time

- Description: a script (TS or Python) parses the TS source, walks the TypeBox AST, emits Python dict literals into a `tools/_schemas.py` file. Source-of-truth stays TS.
- Pros: schemas can't drift; one source of truth.
- Cons:
  - Failure modes: regex-based extraction breaks on subtle syntax (`Type.Optional(Type.String({...}))` nested across lines, template-literal description strings, conditional fields). AST-based extraction requires running TS through a parser at every build.
  - Adds a build-step dependency the Python side currently doesn't have (no Node.js in the Hermes runtime image).
  - The TypeBox→JSON-Schema converter is itself nontrivial (TypeBox is a runtime construct, not a static schema document).
  - For an 8-schema, ~1-hour-of-typing surface, the converter is more code than what it generates.
- Evidence cited: `tools.md:721` — "Translation policy: hand-translate (do not auto-derive). The TS schemas mix TypeBox idioms with hand-written description strings that exceed 200 chars, and several use the older `Type.String({enum})` instead of `Type.Union` — automated translation would lose nuance."

### Option C: Generate at install time via a TypeBox runtime in Python

- Description: write a Python implementation of the TypeBox builders (`Type.String`, `Type.Object`, etc.), import the TS source somehow, run it. Effectively forking TypeBox into Python.
- Pros: theoretical symmetry.
- Cons: rejected on sight. TypeBox is a TS-only library; reimplementing it in Python is a multi-week project to serve a 1-hour problem.

## Decision

Chosen: **Option A (hand-translate)**.

## Rationale

`tools.md` already confirms TypeBox `Type.Strict()` output is identical to JSON Schema — the translation is mechanical at the structural level, only the description prose carries information. With prose copied verbatim from TS source comments (and `tools.md` having already done that copy once), the failure modes of an automated converter (subtle paraphrasing, escape-quote drift, template-literal concatenation glitches) outweigh the benefit of zero-drift guarantee. The ~6,500 LOC is one-time work — schemas change rarely, and when they do, we want a human to read the diff in both languages.

The description strings are the load-bearing artifact here. Wave-12 retro N3 noted that the `truncationNotice` regex is part of the agent-facing contract: model-facing prose earns its keep because the model reads it. Letting a converter touch those strings is the failure mode we're trying to prevent.

## Consequences

- Every `tools/<name>.py` file owns its own `LCM_<NAME>_SCHEMA` dict at module top level. `LCMEngine.get_tool_schemas()` is a one-line `[LCM_GREP_SCHEMA, LCM_DESCRIBE_SCHEMA, …]`.
- Description strings are pasted verbatim from `tools.md`, which itself was lifted verbatim from the TS source. Triple-quoted strings (`"""..."""`) preserve newlines and avoid escape-quote issues with embedded single/double quotes.
- A CI test (`tests/tools/test_schemas_wellformed.py`) loads each schema and asserts `jsonschema.Draft7Validator.check_schema(s) is None` — catches typos at PR time.
- A CI test (`tests/tools/test_schemas_match_ts.py`) compares the loaded Python schemas to a JSON-Schema export from the TS source (run once via a `tsc + node` step in the LCM repo and committed as a fixture under `tests/fixtures/lcm_v4.1_schemas.json`). Catches drift if the TS source moves while we sleep.
- Schema changes require updates in both TS (LCM) and Python (lossless-hermes). PR template adds a checklist entry: "If you touched a `lcm_*` tool schema, did you update the matching `tools/*.py`?"
- This precludes a future "schemas live in YAML, both languages load from the same file" refactor. If we ever want that, we'd revisit this ADR.

## Open questions / 5% uncertainty

- Wave-13+ TS audits may tune description prose further. The fixture-comparison test catches drift but doesn't automatically apply the fix — by design. Mitigation: include the LCM-source-map version pin (`pr-613@1f07fbd`) in the fixture filename so version skew is obvious in diffs.
- TypeBox edge cases not yet hit in v4.1: discriminated unions, conditional refinements, `Type.Recursive`. None of the eight current schemas use these. If a future schema does, the hand-translate policy revisits whether we need a converter for that one — Option A doesn't preclude case-by-case scripting.
