---
name: Port issue
about: Port the three budget-walk selection modes (full-fit / prompt-aware BM25-lite / chronological)
title: '[epic-03] assembler: port budget-walk selection modes'
labels: 'port'
---

## Source (TypeScript)
- File: `src/assembler.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 1162–1230 (the three-branch selection block), with helpers: `tokenizeText` 1037–1042, `scoreRelevance` 1049–1075, `hasSearchablePrompt` (search the file — small predicate).
- Function(s)/class(es): The inline budget-walk inside `assemble` + `scoreRelevance`, `tokenizeText`, `hasSearchablePrompt`.

## Target (Python)
- File: `src/lossless_hermes/assembler.py`
- Estimated LOC: ~150 (function + helpers + tests are extensive)

## Background

After fresh-tail boundary is computed (#03-05) and items are split into `evictable` (`ordinal < fresh_tail_ordinal`) and `fresh_tail` (`>= fresh_tail_ordinal`), the budget walk decides which evictable items to KEEP given the remaining budget after tail allocation.

From `docs/porting-guides/assembler-compaction.md` §"Step-by-step" step 8:

- `tail_tokens = sum(fresh_tail.tokens)`. Tail is always included, even if it alone exceeds budget.
- `remaining_budget = max(0, token_budget - tail_tokens)`.

**Three selection modes** (the `selection_mode` debug field):

### Mode 1: `full-fit` (lines 1181–1184)

`evictable_total_tokens <= remaining_budget`. Keep everything. No eviction.

### Mode 2: `prompt-aware` (lines 1185–1209)

Gate: `prompt_aware_eviction != False AND has_searchable_prompt(prompt)`.

Algorithm:

1. For each evictable item, compute `score = score_relevance(item.text, prompt)`.
2. Sort items by `(score desc, ordinal desc)` — ties broken by recency.
3. Greedy fill: walk sorted list; for each item, if it fits in `remaining_budget`, keep it and decrement budget; else skip.
4. Re-sort kept items by `ordinal` (chronological order restored for output).

### Mode 3: `chronological` (lines 1210–1230, default fallback)

Walk evictable from newest to oldest (descending ordinal). Once an item doesn't fit, **stop entirely** — drops all older items too (not "skip and continue"; the budget walk is monotonic). Reverse for chronological order on output.

## BM25-lite scoring algorithm (`scoreRelevance`, lines 1049–1075)

```python
def _score_relevance(item_text: str, prompt: str) -> float:
    """Maps to assembler.ts:1049–1075.

    TF normalized by item-term-count. One accumulator per UNIQUE prompt term.
    Ties broken by recency (handled in the sort key of the caller).
    """
    item_tokens = _tokenize_text(item_text)
    if not item_tokens:
        return 0.0
    item_term_count = len(item_tokens)

    prompt_tokens = _tokenize_text(prompt)
    if not prompt_tokens:
        return 0.0

    item_freq: dict[str, int] = {}
    for t in item_tokens:
        item_freq[t] = item_freq.get(t, 0) + 1

    score = 0.0
    seen_prompt_terms: set[str] = set()
    for pt in prompt_tokens:
        if pt in seen_prompt_terms:
            continue
        seen_prompt_terms.add(pt)
        if pt in item_freq:
            # Normalize by item-term-count (not by IDF — this is BM25-LITE, not full BM25).
            score += item_freq[pt] / item_term_count
    return score
```

### `tokenizeText` (lines 1037–1042)

```python
def _tokenize_text(text: str) -> list[str]:
    """Maps to assembler.ts:1037–1042.

    Lowercase, split on non-alphanumeric, filter len > 1.
    """
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1]
```

### `hasSearchablePrompt`

Predicate that returns False for `None`, empty string, whitespace-only, or any string whose tokenization yields zero tokens.

## Performance note (per `docs/porting-guides/assembler-compaction.md` §"Token-budget walk")

The TS impl is O(n): one pass to sum `evictableTotalTokens`, one greedy walk, one optional sort for prompt-aware mode. **Ensure the Python port doesn't accidentally O(n²)** — Python's `list.append` + `list.sort` are linear/n-log-n; avoid `list = list + [item]` (quadratic copying) inside the loop.

## Function shape

```python
@staticmethod
def _budget_walk(
    evictable: list[ResolvedItem],
    fresh_tail: list[ResolvedItem],
    token_budget: int,
    prompt: str | None,
    prompt_aware_eviction: bool,
) -> tuple[list[ResolvedItem], str]:
    """Maps to assembler.ts:1162–1230.

    Returns (kept_evictable_items, selection_mode).
    The caller assembles the final list as kept_evictable_items + fresh_tail.
    """
```

## Dependencies
- Depends on: #03-04 (`ResolvedItem` with `tokens` and `text`), #03-05 (boundary is upstream of the split).
- Blocks: #03-08 (orchestration calls this).

## Acceptance criteria

- [ ] All three selection modes are reachable; `selection_mode` return value matches one of `"full-fit"`, `"prompt-aware"`, `"chronological"`.
- [ ] `full-fit`: `evictable_total_tokens <= remaining_budget` → all evictable items returned.
- [ ] `prompt-aware`: scoring matches TS for known fixtures; ties broken by recency (descending ordinal); output re-sorted by ordinal ascending.
- [ ] `chronological`: walks newest→oldest, stops on first non-fitting item; output reversed to chronological order.
- [ ] `prompt_aware_eviction = False` forces `chronological` even with a non-empty prompt.
- [ ] Empty prompt or whitespace-only prompt falls back to `chronological` (via `has_searchable_prompt`).
- [ ] Tail-only over budget: tail is still included (overflow allowed); `remaining_budget = 0`; evictable returns empty unless `evictable_total_tokens == 0`.
- [ ] BM25-lite: `score_relevance` matches TS to within float-precision on a fixture corpus.
- [ ] `tokenize_text` filters empty strings and length-1 tokens (matches TS `t.length > 1`).
- [ ] No quadratic patterns (sort once, append in linear time).
- [ ] All TS unit tests covering the three modes (search `test/assembler*.test.ts` for `selectionMode`, `bm25`, `chronological`) have ported pytest equivalents under `tests/test_assembler_budget_walk.py`.
- [ ] `pytest tests/test_assembler_budget_walk.py` passes locally + on GitHub CI.
- [ ] No new mypy errors.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests

- All three modes: independent fixtures per mode.
- Mode-switch boundaries:
  - `evictable_total_tokens == remaining_budget` (full-fit boundary).
  - `evictable_total_tokens == remaining_budget + 1` (falls to prompt-aware or chronological).
- BM25-lite scoring:
  - Identical token sets → same score regardless of repetition (TF normalized by item-term-count).
  - Unique-prompt-terms invariant: prompt `"foo foo bar"` scores items the same as prompt `"foo bar"`.
  - Item with no matching tokens → score 0.0.
- Tie-breaking: two items with identical scores → newer (higher ordinal) wins.
- Chronological strict-stop: a small item AFTER a too-big item is NOT picked up.
- Empty prompt → mode is `chronological`.
- `prompt_aware_eviction = False` → mode is `chronological` even with prompt.
- Quadratic-perf regression: fixture with 5000 evictable items completes in <100 ms.

## Estimated effort
**12 hours**. The three-branch selection is small but BM25-lite parity with TS is fiddly — plan to port the `tokenize` + `score` helpers first and validate against TS-side reference values before plugging in.

## Confidence
**90%**. Algorithm is fully documented and tested in TS. Residual risk: float-precision drift between TS and Python `+=` accumulation order (mitigated by tolerance bounds in tests, not exact equality).
