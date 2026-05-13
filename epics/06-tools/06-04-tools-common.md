---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port common.ts shared utilities'
labels: 'port'
---

## Source (TypeScript)
- File: `src/tools/common.ts`
- Lines: ~53 LOC
- Function(s)/class(es): `AnyAgentTool` type, `jsonResult(payload)`, `readStringParam(params, key, options)`.

## Target (Python)
- File: `src/lossless_hermes/tools/_common.py` (leading underscore — module-private; not re-exported from `tools/__init__.py`).
- Estimated LOC: ~50 LOC.

## Dependencies
- Depends on: none (foundational helper).
- Blocks: every per-tool handler — they all call `tool_result(payload)` instead of returning structured dicts directly.

## Acceptance criteria
- [ ] `tool_result(payload: dict) -> str` — Hermes's `handle_tool_call` returns a JSON string (not a structured `{content, details}` dict like the TS `AnyAgentTool.execute` shape). Returns `json.dumps(payload, ensure_ascii=False)`.
- [ ] `read_string_param(params: dict, key: str, *, required: bool = False, default: str | None = None) -> str | None`:
  - Returns `default` (or raises `ValueError` if `required=True`) when key is absent.
  - Returns the value coerced to `str` via `str(value).strip()` if present.
  - Empty string after strip → behaves as absent (matches TS `readStringParam` semantics; verify against test).
- [ ] `read_number_param(params: dict, key: str, *, minimum: float | None = None, maximum: float | None = None, default: float | None = None) -> float | None` — numeric coercion helper used by tools that accept `limit`, `windowHours`, `tokenCap`, etc.
- [ ] `read_bool_param(params: dict, key: str, *, default: bool = False) -> bool` — handles `"true"`/`"false"` strings as well as actual bools (some agent providers stringify).
- [ ] Tests pin the empty-string-as-absent behaviour and the strip semantics; missing keys raise descriptively when required.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- `tests/tools/test_common.py` — string/number/bool coercion, missing-required-raises, empty-after-strip-is-absent, min/max clamping for numbers.

## Estimated effort
**2 hours** — small surface; the test cases are the work.

## Confidence
**98%** — trivial port. The 2% is on coercion edge cases that may surface during downstream tool-handler porting.

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "common.ts" section (lines 540–544).
