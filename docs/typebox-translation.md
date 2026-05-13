# TypeBox -> Python dict translation conventions

**Status:** living document
**Owner:** Epic 06 (agent tools)
**Source-of-truth pin:** lossless-claw at commit `1f07fbd`, branch `pr-613`
**Companion ADR:** [ADR-016](./adr/016-typebox-translation.md)
**Companion code:** [`src/lossless_hermes/tools/_typebox.py`](../src/lossless_hermes/tools/_typebox.py)

> **Scope:** how to translate a TypeScript TypeBox schema into a Python
> dict literal for the per-tool ports in 06-07..06-14. This document
> codifies the conventions that the helpers in `_typebox.py`
> operationalize.

## Why hand-translate (not auto-generate)

Per [ADR-016](./adr/016-typebox-translation.md), schemas are
hand-translated from the TypeScript source. The decision rejected
automated generation:

1. **Description prose is load-bearing.** Every `description: "..."` in
   a TypeBox literal is hand-authored model-facing text, tuned across
   LCM Wave-1..Wave-12 audits. The strings encode PRIMARY-vs-secondary
   tool-selection hints, fallback suggestions, env-knob names, and
   parameter caps. An automated converter would silently paraphrase or
   re-escape whitespace; the model would then route differently.

2. **The mechanical part is trivial.** TypeBox's `Type.Strict()` already
   emits standards-compliant JSON Schema. Translating `Type.String({...})`
   to `{"type": "string", ...}` is one-to-one. The risk of an automated
   converter is in the prose, not the structure.

3. **Schema changes are rare and deliberate.** LCM's eight tool schemas
   change roughly once per audit wave. A human reading the diff in both
   languages is the right gate.

The helpers in `_typebox.py` codify the mechanical part so every
per-tool schema follows the same shape; the prose stays inline and
**verbatim from the TS source**.

## The translation table

The canonical mapping is reproduced here from
[`docs/porting-guides/tools.md`](./porting-guides/tools.md) lines
708-721. Each row maps one TypeBox builder to its Python dict form.

| TS form | Python dict | Helper |
|---|---|---|
| `Type.String({description})` | `{"type": "string", "description": ...}` | `string_field("...")` |
| `Type.String({description, enum: [...]})` | `{"type": "string", "enum": [...], "description": ...}` | `string_field("...", enum=[...])` |
| `Type.Number({description, minimum, maximum})` | `{"type": "number", "minimum": ..., "maximum": ..., "description": ...}` | `number_field("...", minimum=..., maximum=...)` |
| `Type.Boolean({description})` | `{"type": "boolean", "description": ...}` | `boolean_field("...")` |
| `Type.Array(Type.X, {description})` | `{"type": "array", "items": <X>, "description": ...}` | `array_field(<X>, description="...")` |
| `Type.Object({props})` | `{"type": "object", "properties": {...}, "required": [...]}` | `object_schema(**props)` |
| `Type.Optional(X)` | Omit key from `required`; keep in `properties` | `optional(<X>)` |
| `Type.Union([Type.Literal("a"), Type.Literal("b")])` | `{"enum": ["a", "b"]}` (rare; most enums use `Type.String({enum})`) | use `string_field(enum=[...])` |

## Verbatim-description rule (load-bearing)

Every `description: "..."` string in a TypeBox property MUST be copied
**byte-identical** from the TS source into the Python dict. Per
[ADR-016 §Rationale](./adr/016-typebox-translation.md):

> The description strings are the load-bearing artifact here. Wave-12
> retro N3 noted that the `truncationNotice` regex is part of the
> agent-facing contract: model-facing prose earns its keep because the
> model reads it.

In practice:

1. **Copy from the TS file**, not from `tools.md` (the porting guide may
   have re-wrapped lines for readability). The TS source at
   `/Volumes/LEXAR/Claude/lossless-claw/src/tools/lcm-<tool>-tool.ts`
   is the canon.

2. **Preserve all punctuation** — apostrophes, smart quotes, em dashes,
   ASCII single quotes inside backtick code fences. Python triple-quoted
   strings (`"""..."""`) avoid escape-quote drift; prefer them over
   `"..."` for any description longer than one line.

3. **Preserve TS template-literal concatenation as a single string.** The
   TS source often splits a long description across `+`-joined lines for
   editor readability:

   ```typescript
   description:
     "Search compacted conversation history. " +
     "PRIMARY for Type B topic-anchored queries: " +
     "'have we ever discussed X'.",
   ```

   The Python form is a single concatenated string — newlines added by
   TS line-splitting are NOT semantic:

   ```python
   description=(
       "Search compacted conversation history. "
       "PRIMARY for Type B topic-anchored queries: "
       "'have we ever discussed X'."
   ),
   ```

   The fixture-match test (when fixture lands; see
   [§Fixture comparison](#fixture-comparison-deferred-until-lcm-export-ships)
   below) treats the joined string as the canonical form.

4. **Template-literal interpolations are concretized.** Some TS schemas
   use `` `... ${CONSTANT} ...` `` to inline a default-value literal:

   ```typescript
   description: `Maximum answer tokens (default: ${DEFAULT_MAX_ANSWER_TOKENS}).`,
   ```

   In Python, paste the resolved string (the constant's runtime value
   at LCM commit `1f07fbd`):

   ```python
   description="Maximum answer tokens (default: 1200).",
   ```

   The TS constant's value is the source-of-truth. If LCM bumps and
   the constant moves, the description shifts too — and the fixture
   match test catches the drift.

## Provenance comment rule

Per [ADR-029](./adr/029-wave-fix-provenance.md) and the AC list of
[issue 06-01](../epics/06-tools/06-01-typebox-translation-conventions.md),
every `LCM_<TOOL>_SCHEMA` dict in the port carries a provenance comment
at the top:

```python
# Verbatim from src/tools/lcm-grep-tool.ts:43-125 (LCM commit 1f07fbd).
LCM_GREP_SCHEMA = tool_schema(
    name="lcm_grep",
    description=(...),
    parameters=object_schema(...),
)
```

The line range is the closed interval covering the TypeBox
`const LcmXxxSchema = Type.Object({...})` block plus the `description:`
literal in the tool factory's `return {...}`. Future readers diffing
LCM bumps look at this range to scope the review.

## `Type.Optional` and `required` ordering

JSON Schema Draft-07 doesn't have an "optional" keyword. Optionality is
expressed at the object level via the `required: [...]` array — any
property NOT in `required` is optional.

TypeBox's `Type.Object` computes `required` by inspecting which property
values are wrapped in `Type.Optional`. The Python `object_schema(...)`
helper does the same: any property passed as `optional(X)` is omitted
from `required`; every other property is required.

**The `required` array is sorted in insertion order** — the order the
property names appear in the `object_schema(...)` keyword arguments.
This matches TypeBox's behavior (it emits `required` in declaration
order). Byte-equality with the TS-exported fixture depends on this
invariant.

## Helpers vs hand-written dict literals

Per-tool modules are free to inline a hand-written dict literal — the
helpers in `_typebox.py` are **conventions, not enforcement**. Use the
helpers when:

- The schema is mostly mechanical (all `Type.String({description})`
  fields with no exotic options). The helpers make the Python read like
  the TS source.

- A description spans 3+ lines. Triple-quoted strings + helpers keep
  the structure flat.

Hand-write the dict literal when:

- A description contains complex string interpolation that's easier to
  build with Python's standard string operations before passing in.

- The schema has a one-off keyword (e.g. `examples`, `default`) not yet
  covered by the helpers. Add the keyword to the helper in a follow-up
  PR if it shows up in 2+ schemas.

## Fixture comparison (deferred until LCM export ships)

[Issue 06-01 §Acceptance criteria](../epics/06-tools/06-01-typebox-translation-conventions.md)
lists a fixture-match test that compares each `LCM_<TOOL>_SCHEMA` to a
JSON-Schema export from the TS source committed as
`tests/fixtures/lcm_v4.1_schemas.json`.

Per the issue's §"5% uncertainty" paragraph, the LCM-side export
tooling does not yet exist — someone has to write the
`tsc + node` step that emits the JSON dump. Until that fixture lands,
the matching test (`tests/tools/test_schemas_match_ts.py`) is marked
`pytest.mark.xfail(strict=False)` with a `reason=` pointing to this
section.

When the fixture lands:

1. The fixture file is named `lcm_<version>_schemas.json` (e.g.
   `lcm_v4.1_schemas.json`). The version segment makes version skew
   obvious in diffs.

2. The fixture contains a top-level dict `{tool_name: <openai-format
   schema>}`. The match test iterates :func:`get_tool_schemas` and
   asserts each entry equals `fixture[entry["name"]]`.

3. Comparison is BYTE-IDENTICAL — no whitespace normalization. This is
   the policy choice ADR-016 §Open-questions left to the
   implementation: we prefer byte-identical so drift in the TS source
   (even pure whitespace) is caught.

4. Drift is fixed by **deliberately re-porting** the description string,
   not by relaxing the test. The whole point of byte-identity is that a
   Wave-13+ TS prose tweak surfaces here.

## Quick reference: porting a single tool

Mechanical checklist for issues 06-07..06-14:

1. **Open the TS source** at
   `/Volumes/LEXAR/Claude/lossless-claw/src/tools/lcm-<tool>-tool.ts`.

2. **Locate the `const LcmXxxSchema = Type.Object({...})` block** and
   the tool-factory `return {name, label, description, parameters}` —
   the description string is at the factory level, not on the schema.

3. **Translate each property** using the table above. Wrap optional
   ones in `optional(...)`. Preserve TS field-declaration order.

4. **Copy descriptions verbatim** — no paraphrasing, no whitespace
   normalization. Use triple-quoted strings for multi-line. Concretize
   template-literal interpolations to the constant's value at LCM
   commit `1f07fbd`.

5. **Write the dict**:

   ```python
   # Verbatim from src/tools/lcm-<tool>-tool.ts:<line range> (LCM commit 1f07fbd).
   LCM_<TOOL>_SCHEMA = tool_schema(
       name="lcm_<tool>",
       description=("..."),
       parameters=object_schema(
           prop_a=string_field("..."),
           prop_b=optional(number_field("...", minimum=1)),
       ),
   )
   ```

6. **Register**: at the bottom of the per-tool module, append:

   ```python
   from lossless_hermes.tools import TOOL_SCHEMAS
   TOOL_SCHEMAS.append(LCM_<TOOL>_SCHEMA)
   ```

7. **Verify**: `pytest tests/tools/test_schemas_wellformed.py -k
   <tool>` — auto-parametrized over the registry.

## Open questions (carried from ADR-016)

1. **Discriminated unions, conditional refinements, `Type.Recursive`** —
   none of the eight v4.1 schemas use these. If a future schema does,
   the hand-translate policy revisits whether we need a one-off
   converter. Option A doesn't preclude case-by-case scripting.

2. **The `lcm_v4.1_schemas.json` fixture is not yet emitted** by the LCM
   build. The match test is `xfail` until the fixture lands. See ADR-016
   §Open-questions row 1.

3. **TypeBox `Type.Union` of `Type.Literal`s** — rare in this surface
   (most enums use `Type.String({enum})`). If we hit one, the policy
   is to translate to `string_field(enum=[...])` and document the
   choice on the per-tool module.
# CI retrigger 7500a9e — empty commits did not propagate to runners
