---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-04] summarize: port 3 prompt templates verbatim'
labels: 'port'
---

## Source (TypeScript)
- File: `lossless-claw/src/summarize.ts` (pr-613 `1f07fbd`)
- Lines:
  - `LCM_SUMMARIZER_SYSTEM_PROMPT`: 59 (~3 LOC)
  - `buildLeafSummaryPrompt`: 881–928 (~48 LOC)
  - `buildCondensedSummaryPrompt` dispatch: 1052–1067 (~16 LOC)
    - `buildD1Prompt`: 930–978 (~49 LOC) — depth ≤ 1
    - `buildD2Prompt`: 980–1014 (~35 LOC) — depth == 2
    - `buildD3PlusPrompt`: 1016–1050 (~35 LOC) — depth ≥ 3
  - `buildDeterministicFallbackSummary`: 1075–1102 (~28 LOC)
- Function(s)/class(es): Module-level functions in `summarize.ts`

## Target (Python)
- File: `src/lossless_hermes/summarize.py`
- Estimated LOC: ~220 (the prompt strings themselves are most of the LOC; helpers are thin)
- Functions:
  - `_build_leaf_prompt(text, mode, target_tokens, previous_summary, custom_instructions) → str`
  - `_build_condensed_prompt(text, target_tokens, depth, previous_summary, custom_instructions) → str` (dispatches to D1/D2/D3+)
  - `_build_d1_prompt(...)`, `_build_d2_prompt(...)`, `_build_d3_plus_prompt(...)` (private helpers)
  - `_build_deterministic_fallback(text, target_tokens) → str`
- Module-level constant: `LCM_SUMMARIZER_SYSTEM_PROMPT`

## Why "verbatim"

Per `docs/porting-guides/assembler-compaction.md` §"Prompt templates":

> Quoted verbatim:
> ```
> You summarize a SEGMENT of an OpenClaw conversation for future model turns.
> Treat this as incremental memory compaction input, not a full-conversation summary.
> ...
> ```

The prompt content is **load-bearing operator tuning**. The exact phrasing has been audited against summarizer output quality. A "cleaner rewrite" will produce subtly different summaries that may pass tests but degrade quality in production. Port the strings byte-for-byte, including:

- Newlines and blank lines (template structure)
- Capitalization of bullet markers (`-`)
- Exact phrase `"Expand for details about:"` end marker
- The literal phrase `"Files: none"` for the file-operations-empty case
- `[<previousSummary or "(none)">]` literal sentinel
- `<previous_context>`, `<conversation_segment>`, `<conversation_to_condense>` XML wrapper tags

## Template 1: Leaf summary

`_build_leaf_prompt(text, mode, target_tokens, previous_summary, custom_instructions)`:

Per the porting guide (lines 334–370), the prompt has 4 sections in order:

1. **Header** (constant):
   ```
   You summarize a SEGMENT of an OpenClaw conversation for future model turns.
   Treat this as incremental memory compaction input, not a full-conversation summary.
   ```
   Per ADR-024 the "OpenClaw" string MUST be preserved — agents see "OpenClaw" in their compaction summaries and we don't want a port-time wording divergence to drift quality. (The agent has no Hermes-vs-OpenClaw distinction at the prompt level.)

2. **Policy block** — dispatch on `mode`:
   - `mode == "normal"` → normal-mode bullets (preserve key decisions, rationale, etc.)
   - `mode == "aggressive"` → aggressive-mode bullets (durable facts + current task state only)

3. **Instruction block** — `custom_instructions or "(none)"`.

4. **Output requirements + previous context + segment** — including:
   - `- Target length: about <target_tokens> tokens or less.` (the int is interpolated)
   - `<previous_context>\n<previous_summary or "(none)">\n</previous_context>`
   - `<conversation_segment>\n<text>\n</conversation_segment>`

## Template 2: Condensed summary

`_build_condensed_prompt(text, target_tokens, depth, previous_summary, custom_instructions)`:

Dispatches by depth (per porting guide lines 372–377):

```python
def _build_condensed_prompt(text, target_tokens, depth, previous_summary, custom_instructions):
    if depth <= 1:
        return _build_d1_prompt(text, target_tokens, previous_summary, custom_instructions)
    if depth == 2:
        return _build_d2_prompt(text, target_tokens, custom_instructions)  # no previous_summary at d2+
    return _build_d3_plus_prompt(text, target_tokens, custom_instructions)
```

**D1** ("leaf-level conversation summaries into a single condensed memory node…") — includes `<previous_context>` block; timeline directive "hour or half-hour".

**D2** ("session-level summaries into a higher-level memory node…") — NO previous_context; timeline directive "dates and approximate time of day".

**D3+** ("high-level memory node from multiple phase-level summaries…") — NO previous_context; timeline directive "date ranges".

All three share:
- Preserve/Drop bullet lists (verbatim from TS)
- `Expand for details about:` end marker
- Target-length line
- Source text wrapped in `<conversation_to_condense>...</conversation_to_condense>`

## Template 3: Deterministic fallback

`_build_deterministic_fallback(text, target_tokens) → str`:

Per the porting guide §"Deterministic fallback" + the Wave-4 P0 fix note:

```python
def _build_deterministic_fallback(text: str, target_tokens: int) -> str:
    # LCM Wave-4 P0 (2026-01-18): ALWAYS prepend a deterministic marker, even when
    # source fits within char budget — operators must be able to distinguish
    # "LLM down" from "LLM ran cleanly."
    # Original: lossless-claw/src/summarize.ts:1075–1102.
    max_chars = max(256, target_tokens * 4)
    if len(text) <= max_chars:
        return (
            "[LCM fallback summary — model unavailable; raw source preserved verbatim below]\n"
            + text
        )
    return (
        "[LCM fallback summary — model unavailable; raw source truncated for context management]\n"
        + text[:max_chars]
    )
```

Note the **em dash (—) NOT hyphen (-)** in the marker — port byte-for-byte from TS.

## System prompt

```python
LCM_SUMMARIZER_SYSTEM_PROMPT = (
    "You are a context-compaction summarization engine. "
    "Follow user instructions exactly and return plain text summary content only."
)
```

(One literal string, no formatting variables.)

## Wave-N fixes to preserve

Per ADR-029, add inline comment at:

- **Wave-4 P0 (deterministic fallback marker always present)** at `_build_deterministic_fallback` — see code above.
- **Wave-9 marker-distinct-from-truncation**: the two marker variants (`preserved verbatim` vs `truncated for context management`) are themselves a Wave-9 fix:
  ```python
  # LCM Wave-9 (2026-03-08): two distinct markers (preserved-verbatim vs truncated)
  # so operators can tell whether truncation also occurred during fallback.
  # Original: lossless-claw/src/summarize.ts:1075–1102.
  ```

## Dependencies
- Depends on: Issue 04-08 (`CompactionResult` dataclass — needed for the `target_tokens` resolution test that exercises the full chain)
- Blocks: Issue 04-06 (fallback chain uses `_build_deterministic_fallback`; main escalation uses `_build_leaf_prompt` + `_build_condensed_prompt`)
- Blocks: Issue 04-07 (circuit breaker integration tests need the prompts as stable fixtures)

## Acceptance criteria
- [ ] All 3 prompt strings match TS byte-for-byte (use a snapshot test fixture, see Tests)
- [ ] Em dash (—) NOT hyphen (-) in deterministic-fallback markers
- [ ] `LCM_SUMMARIZER_SYSTEM_PROMPT` exact match
- [ ] `_build_leaf_prompt(mode="normal")` produces different text than `_build_leaf_prompt(mode="aggressive")` (policy block dispatch works)
- [ ] `_build_condensed_prompt(depth=0)` uses D1; `depth=2` uses D2; `depth=4` uses D3+ (dispatch matrix)
- [ ] D1 includes `<previous_context>`; D2 and D3+ do NOT
- [ ] `_build_deterministic_fallback` ALWAYS includes a marker — even for short text where `len(text) <= max_chars`
- [ ] `_build_deterministic_fallback` `max_chars = max(256, target_tokens * 4)` (NOT just `target_tokens * 4` — the 256 floor is load-bearing for very small targets)
- [ ] Wave-4 + Wave-9 inline comments present per ADR-029
- [ ] All TS unit tests in `test/summarize.test.ts` (the "prompt template" describe blocks) ported
- [ ] PR description cites LCM commit SHA `1f07fbd`

## Tests

Port from `test/summarize.test.ts`:

### Snapshot tests (verbatim verification)

```python
def test_leaf_prompt_normal_snapshot():
    prompt = _build_leaf_prompt(
        text="Hello world",
        mode="normal",
        target_tokens=600,
        previous_summary=None,
        custom_instructions=None,
    )
    # Pin every byte. A change here is intentional and PR-reviewed.
    assert prompt == EXPECTED_LEAF_NORMAL_PROMPT  # full string in tests/_fixtures/

def test_leaf_prompt_aggressive_snapshot(): ...
def test_d1_prompt_snapshot(): ...
def test_d2_prompt_snapshot(): ...
def test_d3_plus_prompt_snapshot(): ...
def test_deterministic_fallback_marker_always_present(): ...
```

### Behavioral tests

- `_build_leaf_prompt with previous_summary` includes the text inside `<previous_context>`
- `_build_leaf_prompt without previous_summary` includes literal `"(none)"`
- `_build_leaf_prompt with custom_instructions` substitutes the instruction text
- `_build_condensed_prompt(depth=0)` and `_build_condensed_prompt(depth=1)` both produce D1 output
- `_build_condensed_prompt(depth=2)` produces D2 (different from D1)
- `_build_condensed_prompt(depth=3)` and `depth=4` both produce D3+ (same template, depth not interpolated at depth≥3)
- `_build_deterministic_fallback` truncation: text of `target_tokens * 4 + 100` chars truncated to `target_tokens * 4`
- `_build_deterministic_fallback` 256 floor: `target_tokens=10` → `max_chars=256` not 40

## Estimated effort
6–8 hours (verbatim copy is fast; snapshot fixture maintenance is the long pole)

## Confidence
95% — pure string handling. Risk is in the verbatim copy: an accidental smart-quote/em-dash conversion when copy-pasting from the TS source.
