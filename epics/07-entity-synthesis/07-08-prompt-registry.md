---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] synthesis: port prompt-registry + seed-default-prompts (740 LOC)'
labels: 'port, epic-07'
---

## Source (TypeScript)

- File: `src/synthesis/prompt-registry.ts` (305 LOC) — registry CRUD + lookups
- File: `src/synthesis/seed-default-prompts.ts` (435 LOC) — the 10–11 default prompt rows from architecture-v4.1.md §12 Appendix A
- Lines: 740 LOC combined
- Function(s)/class(es):
  - `register_prompt(conn, opts: RegisterPromptOptions) → str` — `BEGIN IMMEDIATE`, append-only versioning, flip-prev-active
  - `get_active_prompt(conn, *, memory_type, tier_label, pass_kind) → PromptRecord | None`
  - `get_prompt_by_id(conn, prompt_id) → PromptRecord | None`
  - `list_active_prompts(conn) → list[PromptRecord]`
  - `bump_bundle_version(conn) → int`
  - `seed_default_prompts(conn) → dict[str, int]` (returns `{seeded: N, skipped: N}`)
  - `PromptRecord` dataclass

## Target (Python)

- Files:
  - `src/lossless_hermes/synthesis/prompt_registry.py` (~340 LOC)
  - `src/lossless_hermes/synthesis/seed_prompts.py` (~470 LOC — most is the literal prompt text)
- Estimated LOC total: ~810

## What this issue covers

Two co-dependent modules:

### `prompt_registry.py`

Append-only versioning over the `lcm_prompt_registry` table:

- **`register_prompt(conn, opts)`** opens `BEGIN IMMEDIATE`, computes `max(version)+1` for the triple `(memory_type, tier_label, pass_kind)` (counting active + archived rows), flips the previous-active row to `active=0`, inserts the new row with `active=1`, commits. Old versions are never deleted — they stay for traceability + so cache rows pointing at an archived `prompt_id` can still resolve via `get_prompt_by_id`.
- **`get_active_prompt`** returns the highest-version active row for the triple. Empty-string `tier_label` is normalized to NULL (matches the COALESCE-based UNIQUE index).
- **`get_prompt_by_id`** is the exact-by-PK lookup used by cache reads to resolve the prompt that produced cached text, even if archived.
- **`list_active_prompts`** is the operator surface for `/lcm health`.
- **`bump_bundle_version`** atomically `UPDATE active=1 rows SET bundle_version = bundle_version + 1`. Triggers voice-consistency rebuild across the synthesis tier when a prompt set is updated as a coordinated unit.

Wave-discipline:

- **Group D adversarial Gap 3:** empty-string `tier_label` normalizes to NULL in BOTH `get_active_prompt` AND `register_prompt`. The schema's NULL-safe UNIQUE uses `COALESCE(tier_label, '')`, but Python callers must NOT pass `""` and expect the wildcard match; normalize at the boundary.
- **`prompt_id` format:** `pr_<6 hex chars from random>` (~24 bits, ~16M space). Use `secrets.token_hex(3)`. Surface PK violations as `PromptRegistryError("collision")` rather than swallowing them.

### `seed_prompts.py`

The 10–11 default prompts from architecture-v4.1.md §12 Appendix A. Per `synthesis.md`:

1. `episodic-leaf / null / single` — the universal leaf summarizer
2. `episodic-condensed / daily / single`
3. `episodic-condensed / weekly / single`
4. `episodic-condensed / monthly / single`
5. `episodic-condensed / monthly / verify_fidelity`
6. `episodic-yearly / yearly / single` (one of 3 best-of-N candidates)
7. `episodic-yearly / yearly / best_of_n_judge`
8. `episodic-condensed / custom / single` (used by `lcm_synthesize_around`)
9. `episodic-condensed / filtered / single` (grep-filtered path)
10. `procedural-extract / null / single` (strict JSON output)
11. `entity-extract / null / single` (strict JSON array output)

Idempotency: skip-if-any-row-exists for the triple. Operator overrides are NEVER clobbered. Implemented as **raw `INSERT`** (NOT `register_prompt`) so it runs INSIDE the outer migration transaction without a nested-`BEGIN` error.

The seeded prompts MUST NOT contain `{{date_range}}` or `{{target_length}}` placeholders — the renderer doesn't substitute those, so they'd ship verbatim to the LLM. This is Wave-9 Agent #2/#7 P1. Inline `# LCM Wave-9 (2026-03-08): no {{date_range}} or {{target_length}} placeholders` comment on each prompt template.

ADR-005 option A integration: the migration in Epic 01-06 accepts a `seed_default_prompts: Callable | None = None` parameter. This issue wires `seed_prompts.seed_default_prompts` as the callable.

## Dependencies

- Depends on: Epic 01-06 (`lcm_prompt_registry` table + UNIQUE indexes), 07-05 (`PassKind` and `MemoryType` literal types — keep them in `dispatch.py` or move to a shared `types.py`; recommend `synthesis/types.py` for circular-import safety)
- Blocks: 07-05 (`get_active_prompt` is called per synthesis), 07-06 (`prompt_id` flows into the cache key), 07-09 (audit row's `prompt_id` FK)

## Acceptance criteria

- [ ] `register_prompt` opens `BEGIN IMMEDIATE`; computes `max(version)+1`; flips previous-active to `active=0`; inserts new active row; commits as one transaction
- [ ] `get_active_prompt` and `register_prompt` both normalize empty-string `tier_label` to NULL (Group D Gap 3)
- [ ] `get_prompt_by_id` returns archived rows (no filter on `active`)
- [ ] `list_active_prompts` returns one row per triple
- [ ] `bump_bundle_version` is a single atomic UPDATE; returns the new bundle_version (post-bump)
- [ ] `seed_default_prompts` is idempotent: two consecutive calls report `{seeded: N, skipped: 0}` and `{seeded: 0, skipped: N}`
- [ ] Seeded prompts contain ZERO `{{date_range}}` or `{{target_length}}` placeholders (Wave-9 P1)
- [ ] Seeded prompts cover all 11 (memory_type, tier_label, pass_kind) triples listed above
- [ ] Operator override is preserved: pre-existing custom row for `(episodic-leaf, null, single)` is NOT clobbered on next seed call
- [ ] `seed_default_prompts` uses raw `INSERT` (not `register_prompt`) so it composes with the outer migration tx
- [ ] `prompt_id` is `pr_<6 hex chars>` from `secrets.token_hex(3)`
- [ ] PK collision raises `PromptRegistryError("collision")` rather than `sqlite3.IntegrityError`
- [ ] `# LCM Wave-9 (2026-03-08): ...` comment on each seeded template
- [ ] `pytest tests/synthesis/test_prompt_registry.py` and `tests/synthesis/test_seed_prompts.py` pass
- [ ] No new mypy errors with strict mode

## Tests to port

| Source | LOC | Cases |
|---|---:|---|
| `test/prompt-registry.test.ts` | ~200 | (1) register first version → active=1, version=1; (2) register second version → first flipped to active=0, second is active=1, version=2; (3) `get_active_prompt` returns highest-version active row; (4) `get_prompt_by_id` returns archived rows; (5) empty `tier_label` normalizes to NULL on register; (6) empty `tier_label` normalizes to NULL on lookup; (7) bundle-version bump atomic; (8) `list_active_prompts` one-per-triple |
| `test/seed-default-prompts.test.ts` | ~150 | (9) two consecutive seeds → second is no-op; (10) all 11 triples present after seed; (11) operator override preserved; (12) seeded templates have NO `{{date_range}}` or `{{target_length}}` placeholders (Wave-9 P1); (13) raw INSERT path composes with outer migration tx (run inside `BEGIN`/`COMMIT`) |

## Estimated effort

**6–8 hours.** The registry CRUD is straightforward `BEGIN IMMEDIATE` + `INSERT/UPDATE` plumbing. The seed module is mostly literal prompt text — ~400 LOC of triple-quoted strings, plus assertions that they don't contain the forbidden placeholders. Budget ~2 h for the parity test that compares seeded templates byte-for-byte against a vendored TS fixture.

## Confidence

**92%.** Residual risk:

- **`BEGIN IMMEDIATE` semantics across `sqlite3` vs `apsw`** — apsw's autocommit model differs. Both DO support `BEGIN IMMEDIATE`, but apsw's transaction tracking is at the connection level. ADR-004 picks stdlib `sqlite3` primary, `apsw` opt-in extra; cover both in CI matrix.
- **Storing prompt templates** — TS embeds them as TypeScript template literals; Python equivalent is triple-quoted strings. Indentation of the literal must be preserved; use `textwrap.dedent` carefully OR keep literals left-aligned. Caught by byte-equality test against TS fixture.
