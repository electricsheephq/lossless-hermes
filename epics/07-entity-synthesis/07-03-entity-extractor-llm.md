---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] extraction: port entity-extractor-llm (234 LOC, Wave-4 prompt-injection defense)'
labels: 'port, epic-07, wave-4'
---

## Source (TypeScript)

- File: `src/extraction/entity-extractor-llm.ts`
- Lines: 234 LOC
- Function(s)/class(es): `createEntityExtractorLlm({ deps, model?, timeoutMs? }) → ExtractEntitiesFn`, `buildExtractionPrompt(content, tokenCount, fenceToken) → string`, `parseEntityExtractionResponse(raw: string) → ExtractedEntity[]`, `ExtractEntitiesFn` Protocol

## Target (Python)

- File: `src/lossless_hermes/extraction/llm_extractor.py`
- Estimated LOC: ~270

## What this issue covers

The LLM-call adapter for the entity-extraction worker (consumed by 07-02). Three pieces:

1. **`build_extraction_prompt(content, token_count, fence_token)` — verbatim port** of the v4.1 prompt template, complete with random per-call `fence_token` (12 hex chars, 48 bits of entropy) and explicit user-input-boundary markers. Wave-4 Auditor #12 P0-2 hardened this against prompt injection; the inline `# LCM Wave-4 (2026-01-12): ...` comment is mandatory per ADR-029.

2. **Defense-in-depth pre-filter** — before sending to the LLM, refuse extraction (return `[]`) if the leaf content contains an XML-envelope-like pattern: `re.search(r"</?leaf-content-[a-f0-9]{8,}", content, re.I)` or `re.search(r"</leaf-content-", content, re.I)`. This is the Wave-7 final landing of Wave-4 P0-2 #2. Log a `warn` so operators can see which leaves were skipped.

3. **`parse_entity_extraction_response(raw)` — tolerant parser:**
   1. Empty/non-string → `[]`
   2. Trim, strip leading/trailing markdown code fences (` ```json ... ``` `)
   3. Slice between the first `[` and last `]` — handles LLMs that wrap with prose
   4. `json.loads`; on exception or non-list → `[]`
   5. Per entry: require `surface` and `entityType` as non-empty trimmed strings. Normalize `entityType` to snake_case via `re.sub(r"[^a-z0-9_]+", "_", t.lower()).strip("_")`. Drop entries where normalized type is empty. Preserve optional `canonicalText`.

4. **`create_entity_extractor_llm` factory** — binds an `ExtractEntitiesFn` over the injected `deps.complete` (or its Hermes-adapter equivalent). Config:
   - **Model:** `LCM_SUMMARY_MODEL` env, default `"gpt-5.4-mini"` (same default as leaf summarizer)
   - **Timeout:** 30s per call
   - **`max_output_tokens`:** 1024
   - **`pass_kind`:** `"single"` (worker-llm dispatch skips best-of-N judging)
   - **Input cap (`HARD_CAP = 16_000`):** truncate with `"…"` suffix and log `warn` so operators can see which leaves had tail content unseen.

The `fence_token` MUST be a fresh 12-hex-char string per call (`secrets.token_hex(6)` or `uuid.uuid4().hex[:12]`). 48-bit entropy gives ~2^-48 ≈ 4×10^-15 probability of an attacker forging the closing tag.

## Dependencies

- Depends on: Epic 04 (LLM client adapter shape — the `deps.complete` signature this binds over)
- Blocks: 07-02 (coreference worker injects the produced `ExtractEntitiesFn`)

## Acceptance criteria

- [ ] `build_extraction_prompt` returns the exact TS template string with `${fenceToken}` interpolated in three places (open tag, `(${fenceToken})` mid-prose, close tag); whitespace and entity-type example block preserved byte-for-byte
- [ ] `fence_token` is fresh per call, 12 hex chars from `secrets.token_hex(6)` (not `uuid.uuid4().hex[:12]` — the latter has lower entropy from the version/variant bits)
- [ ] Pre-filter regex matches both `<leaf-content-XXXXXXXX>` and `</leaf-content-` patterns (case-insensitive); on match, return `[]` AND log warn with `summary_id` + redacted-snippet for forensics
- [ ] `HARD_CAP = 16_000` enforced; truncated content gets `"…"` suffix; log warn includes `original_len` and `summary_id`
- [ ] `parse_entity_extraction_response` passes all 11 test cases from `test/v41-entity-extractor-llm.test.ts`
- [ ] `entityType` snake_case normalization drops entries that normalize to empty string
- [ ] `canonicalText` preserved when present, omitted when absent (not stored as `None` masquerading as a value)
- [ ] `# LCM Wave-4 (2026-01-12): prompt-injection defense ...` inline comment present on the prompt template AND on the pre-filter regex (per ADR-029)
- [ ] `pytest tests/extraction/test_llm_extractor.py` passes (11 parser cases + prompt-template-equality + pre-filter cases)
- [ ] No new mypy errors with strict mode

## Tests to port

| Source | LOC | Cases |
|---|---:|---|
| `test/v41-entity-extractor-llm.test.ts` | 86 | (1) pure JSON array; (2) fenced ` ```json ... ``` `; (3) fenced without lang tag; (4) prose-wrapped `Here are the entities: [...]`; (5) non-JSON → `[]`; (6) non-array (object) → `[]`; (7) entries missing `surface` dropped; (8) entries missing `entityType` dropped; (9) snake_case normalization (`"PR Number"` → `"pr_number"`); (10) `canonicalText` preserved; (11) trim leading/trailing whitespace |
| New tests this issue adds | — | Wave-4 prompt-template byte-equality; Wave-4 pre-filter `<leaf-content-XXXX>` rejection; Wave-4 pre-filter `</leaf-content-` rejection; `HARD_CAP` truncation; fence-token-fresh-per-call |

## Estimated effort

**4–6 hours.** Pure-function parser ports trivially; the prompt template is a verbatim copy with `f"..."` substitution; the pre-filter is two regexes. Most of the cost is the byte-equality test against a vendored TS-produced prompt fixture.

## Confidence

**92%.** Residual risk:

- **`deps.complete` adapter shape** — Hermes-side LLM client may differ from OpenClaw's `pi-ai`. Mitigation: the `LlmCall` Protocol lives in `synthesis/dispatch.py` (07-05) and is re-used here; both modules pull from the same adapter (lands in Epic 04). If Epic 04's adapter isn't ready, ship a stub `ExtractEntitiesFn` that raises `NotImplementedError` and unblock 07-02.
- **UTF-16-vs-codepoint divergence** in any future `surface_hash_for_id` cross-language compatibility — orthogonal to this issue (lives in 07-02) but the prompt template's content-truncation is character-count-based, not byte-count-based; CJK content cap will land at fewer characters than the TS implementation if `content[:HARD_CAP]` is used over `content[:HARD_CAP].encode()[:HARD_CAP*4]`. Test with a CJK-heavy fixture.
