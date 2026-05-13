# ADR-021: Token estimator

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

LCM has a code-point-aware token estimator at `src/estimate-tokens.ts` (~80 LOC). It walks each Unicode code point in the text and weights by category: CJK 1.5×, emoji 2×, ASCII 0.25×, etc. The comment in `estimate-tokens.ts` notes that a naive char-count formula underestimates CJK by ~6×, which would cause compaction to trigger too late on multilingual sessions and overflow context.

Hermes has a separate function `_content_length_for_budget` in `agent/context_engine.py` that returns a char-length proxy with a fixed `_IMAGE_CHAR_EQUIVALENT = 1600 * 4 = 6400` chars per image. It exists to budget multimodal turns (a screenshot heavy session has ~5 images, naive char count is near-zero).

Both estimators exist, both are "token estimators", and they're fitted to different purposes:
- LCM's: compaction-trigger decisions, summary-cap enforcement, retry-budget math. CJK accuracy is load-bearing because compaction drives the entire pipeline.
- Hermes's: total-context-window budgeting in a one-call assemble step, where images are the dominant unhandled term.

The question: in the Python port, do we (a) port LCM's verbatim, (b) borrow Hermes's, or (c) hybridize?

The user's brief: "Don't borrow Hermes's `_content_length_for_budget` even though it exists. Wave-N fixes to the estimator are load-bearing for retry/budget decisions. Hermes's estimator is for a different purpose. Forking risk is real; better to port verbatim."

## Options considered

### Option A: Port `estimate-tokens.ts` to Python verbatim and use it everywhere LCM previously used it

- Description: `src/lossless_hermes/estimate_tokens.py` is a near-1:1 port of the TS module — ~80 LOC, no deps, pure function. Same code-point walking, same per-category weights, same `truncate_text_to_estimated_tokens(text, max_tokens)` helper. Every LCM call site that used `estimateTokens(...)` in TS gets `estimate_tokens(...)` in Python. Hermes's `_content_length_for_budget` is not invoked from within lossless-hermes code (Hermes may use it for its own assembler; our plugin doesn't reach for it).
- Pros:
  - Wave-N audit fixes stay intact. The TS estimator has been tuned across at least Waves 1, 4, 9, 12 — each fix encodes a regression that landed in production. Forking forgets those.
  - Single source of truth inside the LCM port. Every consumer (compaction trigger, summary cap, retry budget, token-gate estimator in `needs-compact-gate.ts`) uses the same function.
  - Tests for the estimator are directly portable from `test/estimate-tokens.test.ts`.
  - Zero risk of behavioral divergence with the TS source — if LCM behaves a certain way on a fixed input, the Python port behaves identically.
- Cons:
  - Lossless-hermes doesn't share the estimator with Hermes's host code. A multimodal turn in Hermes's pre-LCM assembly path uses one estimator; once LCM gets involved, the budget uses a different one. Mitigated: this is fine — LCM's calculations are internal to its own pyramid; Hermes's pre-assembly uses whatever it always used.
- Evidence cited:
  - `assembler-compaction.md`: "Wave-N fixes to the estimator are load-bearing for retry/budget decisions. Hermes's estimator is for a different purpose. Forking risk is real."
  - LCM `estimate-tokens.ts` comment: naive char-count underestimates CJK by 6×.

### Option B: Use Hermes's `_content_length_for_budget` everywhere

- Description: replace every LCM `estimateTokens` call with Hermes's char-length proxy.
- Pros: one estimator across host + plugin.
- Cons:
  - Loses CJK weighting — compaction would fire too late on multilingual sessions.
  - Loses emoji weighting — emoji-heavy turns underestimated.
  - Wave-fix history is forgotten; regressions that the TS estimator caught now have a different shape.
  - The TS retry-budget math at `summarize.ts` is calibrated to LCM's numbers; switching estimators silently shifts the boundary conditions.

### Option C: Hybrid — LCM's estimator for text, Hermes's image-weighting for multimodal arrays

- Description: in Python, write a wrapper that dispatches:
  - String content → `_estimate_text_tokens(text)` (LCM port).
  - List content → sum: text parts via LCM port; image parts via `_IMAGE_TOKEN_ESTIMATE = 1600`.
- Pros: handles multimodal content correctly in computer-use sessions while preserving CJK accuracy for text.
- Cons:
  - User's brief explicitly says don't borrow Hermes's estimator.
  - The image-weighting question is real but lives at the assembler boundary, not in `estimate_tokens`. The cleanest split is: `estimate_tokens(text)` does text; the assembler handles per-part dispatch (including image counting) on top.
  - The hybrid blends two estimators' calibrations and creates a third behavioral identity — testing surface triples.

Per the user's instruction, Option C is closed even though `assembler-compaction.md` separately recommends it. The instruction is the more recent and authoritative signal.

## Decision

Chosen: **Option A (port `estimate-tokens.ts` verbatim and use it everywhere LCM previously did)**.

## Rationale

The TS estimator is hardened across multiple audit waves. Its weights aren't arbitrary — Wave-4 P0 in `summarize.ts` documents that the `summaryMaxOverageFactor` is calibrated against this estimator's CJK weights. Wave-9 audit fixes to the deterministic-fallback marker depend on `estimateTokens(source) > target * factor` resolving consistently. If we swap the estimator, these threshold conditions get redrawn silently — a regression that's hard to detect because the tests pass on ASCII fixtures.

Hermes's estimator is fit for Hermes's purpose (pre-LCM total budget including images). It does NOT replace LCM's estimator for LCM's purposes. The two coexist; lossless-hermes uses the LCM port internally.

Forking is the real risk per the user's brief. The 80 LOC of `estimate-tokens.ts` is one-time work. Maintaining a forked estimator that diverges from TS audit history would be substantially more work and would lose the provenance trail.

## Consequences

- New file: `src/lossless_hermes/estimate_tokens.py`. Public API:
  - `estimate_tokens(text: str) -> int`
  - `truncate_text_to_estimated_tokens(text: str, max_tokens: int) -> str`
- Every LCM call site that used `estimateTokens` in TS uses `estimate_tokens` in Python. Specifically:
  - Compaction trigger evaluation (`compaction.py: evaluate`, `evaluate_leaf_trigger`).
  - Summary-cap enforcement (`summarize.py: cap_summary_text`, `resolve_target_tokens`).
  - Assembler item-token computation (`assembler.py: resolve_message_item`, `resolve_summary_item`).
  - Token-gate estimator (`needs_compact_gate.py: estimate_result_tokens`).
  - Retry-budget math in `summarize.py`.
- Hermes's `_content_length_for_budget` is not imported. The host's own assembly path continues to use it; the LCM port is a separate budgeting domain.
- Image weighting is a separate concern handled at the assembler level (a multimodal part contributes a fixed +N tokens, configurable). Does NOT fold into `estimate_tokens` per the user's instruction.
- A regression test (`tests/test_estimate_tokens.py`) ports `test/estimate-tokens.test.ts` fixture-for-fixture. Includes CJK strings (Chinese, Japanese, Korean), emoji ZWJ sequences, combining marks, and the specific examples from the TS test bench.
- A CJK-fixture sanity test asserts that `estimate_tokens("中文测试")` and analogous TS-side `estimateTokens("中文测试")` agree to ±1 token. This catches code-point iteration bugs (per `assembler-compaction.md` risk item §8: "TS `for (const char of text)` iterates Unicode code points (surrogate-pair-aware). Python 3's `for char in text` iterates code points natively in CPython").
- Precludes a future "use Hermes's estimator everywhere" refactor unless a follow-up ADR supersedes this one. Acceptable.

## Open questions / 5% uncertainty

- **Surrogate-pair / combining-mark parity.** Spike-004 noted that `for c in text` in CPython iterates code points like TS's `for (const char of text)`, but per-character `ord(c)` may diverge from TS's `codePointAt(0)` on combining marks. Mitigation: a regression test with combining-mark fixtures (`á` = "á") asserting Python and TS agree.
- **Performance.** The TS estimator is O(N) over code points. Python's `ord()` per character is also O(N) but slower per-char than V8. For 10K-char inputs the difference is sub-millisecond. If it ever shows up in profiling, consider a Cython or `numpy` vectorization — but don't preemptively optimize.
- **What if a Wave-13+ audit changes the TS estimator?** The Python port re-ports verbatim. Treat estimator changes as deliberate cross-language ports gated on a PR checklist.
