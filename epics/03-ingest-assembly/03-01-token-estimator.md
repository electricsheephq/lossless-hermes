---
name: Port issue
about: Port `estimate-tokens.ts` to Python per ADR-021
title: '[epic-03] estimator: port code-point-aware token estimator'
labels: 'port'
---

## Source (TypeScript)
- File: `src/estimate-tokens.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: ~80 LOC
- Function(s)/class(es): `estimateTokens(text)`, `truncateTextToEstimatedTokens(text, maxTokens)`

## Target (Python)
- File: `src/lossless_hermes/estimate_tokens.py`
- Estimated LOC: ~90 (Python is slightly more verbose for the per-codepoint switch)

## Background

LCM uses a code-point-aware token estimator that walks each Unicode code point and weights by category (CJK 1.5×, emoji 2×, ASCII 0.25×, …). The TS comment notes naive char-count underestimates CJK by ~6×, which would cause compaction to trigger too late and overflow.

Per **ADR-021**, port the LCM estimator verbatim and use it everywhere LCM previously used it. **Do NOT** borrow Hermes's `_content_length_for_budget` — it is fit for a different purpose (multimodal budgeting in the pre-LCM assembly path).

## Public API

```python
def estimate_tokens(text: str) -> int: ...
def truncate_text_to_estimated_tokens(text: str, max_tokens: int) -> str: ...
```

`estimate_tokens` accepts a string. (Image-weighting at the assembler boundary is a separate concern — `assembler.py` dispatches per-part on multimodal content and calls `estimate_tokens` only on text parts.)

## Dependencies
- Depends on: Epic 00 (scaffolding — `src/lossless_hermes/` package must exist).
- Blocks: every other issue in Epic 03 (assembler, compaction, summarize, retry-budget math all import this).

## Acceptance criteria

- [ ] `estimate_tokens` walks code points (Python's `for c in text` iterates code points natively in CPython).
- [ ] Per-category weights match TS source verbatim (CJK 1.5×, emoji 2×, ASCII 0.25×, default 1×).
- [ ] `truncate_text_to_estimated_tokens` matches TS semantics: returns a string whose `estimate_tokens(result) <= max_tokens`.
- [ ] All TS unit tests in `test/estimate-tokens.test.ts` have ported pytest equivalents under `tests/test_estimate_tokens.py`, fixture-for-fixture.
- [ ] Includes a CJK parity test: `estimate_tokens("中文测试")` agrees with the TS-side value to ±1 token (per ADR-021 risk §"Surrogate-pair / combining-mark parity").
- [ ] Includes emoji ZWJ sequence test and combining-mark test (`"á"` as both NFC and NFD).
- [ ] `pytest tests/test_estimate_tokens.py` passes locally + on GitHub CI.
- [ ] No new mypy errors (strict mode per ADR-008).
- [ ] PR description cites the LCM commit SHA being ported (`1f07fbd` for `pr-613` HEAD).

## Tests

Port `test/estimate-tokens.test.ts` line-for-line to `tests/test_estimate_tokens.py`. Add at minimum:

- ASCII-only short string.
- ASCII-only paragraph (~500 chars).
- Pure CJK string (Chinese, Japanese, Korean separately).
- Emoji-only string with ZWJ sequence (e.g., 👨‍👩‍👧‍👦).
- Mixed ASCII + CJK + emoji.
- Combining-mark fixture (NFC vs NFD `"á"`).
- `truncate_text_to_estimated_tokens` for boundary at exactly `max_tokens`, just over, just under.

## Estimated effort
**6 hours**.

## Confidence
**95%**. Pure function, no dependencies, well-documented source. The only residual risk is the Python/TS code-point parity on combining marks — covered by the regression test.
