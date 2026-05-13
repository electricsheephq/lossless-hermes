"""Code-point-aware token estimator (ADR-021).

Port of ``lossless-claw/src/estimate-tokens.ts`` (LCM commit ``1f07fbd``,
~80 LOC TS → ~80 LOC Python). Uses code-point-aware weighting instead of
naive char-count:

* CJK (Chinese / Japanese / Korean) characters: ~1.5 tokens / char.
* Emoji / Supplementary Plane code points (> U+FFFF): ~2 tokens / char.
* ASCII / Latin / default: ~0.25 tokens / char (≈ 4 chars / token).

### Why not ``len(text) / 4``?

In TypeScript, ``String.length`` counts UTF-16 code units, not Unicode
code points. CJK characters are 1 UTF-16 unit but ~1.5 tokens; emoji are
2 UTF-16 units (surrogate pairs) but ~2-4 tokens. The naive formula
underestimates CJK by ~6× and emoji by ~2-4×, causing compaction to
trigger far too late for non-English conversations.

### TS vs. Python divergence

In Python 3, ``len(text)`` and iteration via ``for c in text`` are
*code-point-aware* natively in CPython — there is no UTF-16 surrogate-
pair concern. A single emoji like ``"🔥"`` is ``len == 1`` in Python
but ``length == 2`` in JS. This means the *input* to the estimator has
the same shape in both languages (one code point per iteration), so the
per-character weighting math is identical. The estimator-level output
matches the TS source to ±1 token on every fixture in the TS test
bench (see ``tests/test_estimate_tokens.py``).

A combining-mark sequence like NFD ``"á"`` (= ``"á"``) iterates as
two code points in both TS and Python; the estimator counts them as
two ASCII-weighted characters in both. The NFC form ``"á"`` is one
code point and counts as one. Caller-side normalization (if needed) is
out of scope.

### Public API

* :func:`estimate_tokens(text)` — total estimated tokens (rounded up via
  ``math.ceil``).
* :func:`truncate_text_to_estimated_tokens(text, max_tokens)` — return a
  prefix slice whose ``estimate_tokens(result) <= max_tokens``.

See:

* ``docs/adr/021-token-estimator.md`` — port-verbatim decision; do
  NOT borrow Hermes's ``_content_length_for_budget``.
* ``lossless-claw/src/estimate-tokens.ts`` — TS source pinned at
  commit ``1f07fbd``.
"""

from __future__ import annotations

import math

__all__ = [
    "estimate_tokens",
    "truncate_text_to_estimated_tokens",
]


def _is_cjk_code_point(cp: int) -> bool:
    """Return True if ``cp`` falls in any CJK / CJK-adjacent Unicode block.

    Block list matches TS source ``isCjkCodePoint`` verbatim (see
    ``estimate-tokens.ts`` lines 18-32 at LCM commit ``1f07fbd``).
    """
    return (
        (0x4E00 <= cp <= 0x9FFF)  # CJK Unified Ideographs
        or (0x3400 <= cp <= 0x4DBF)  # CJK Extension A
        or (0x20000 <= cp <= 0x2A6DF)  # CJK Extension B
        or (0x2A700 <= cp <= 0x2B73F)  # CJK Extension C
        or (0x2B740 <= cp <= 0x2B81F)  # CJK Extension D
        or (0x2B820 <= cp <= 0x2CEAF)  # CJK Extension E
        or (0x2CEB0 <= cp <= 0x2EBEF)  # CJK Extension F
        or (0x3000 <= cp <= 0x303F)  # CJK Symbols and Punctuation
        or (0x3040 <= cp <= 0x30FF)  # Hiragana + Katakana
        or (0xAC00 <= cp <= 0xD7AF)  # Hangul Syllables
        or (0xFF00 <= cp <= 0xFFEF)  # Fullwidth Forms
    )


def _estimate_code_point_tokens(cp: int) -> float:
    """Per-code-point token weight.

    Mirrors TS ``estimateCodePointTokens``: CJK → 1.5; supplementary
    plane (cp > 0xFFFF) → 2; default → 0.25.
    """
    if _is_cjk_code_point(cp):
        return 1.5
    if cp > 0xFFFF:
        return 2.0
    return 0.25


def estimate_tokens(text: str) -> int:
    """Estimate token cost of ``text`` using code-point-aware weighting.

    Iterates Unicode code points (Python ``for c in text`` is code-point-
    aware in CPython 3.x), applies per-category weights, returns the
    rounded-up sum. Empty string returns 0.
    """
    tokens = 0.0
    for char in text:
        tokens += _estimate_code_point_tokens(ord(char))
    return math.ceil(tokens)


def truncate_text_to_estimated_tokens(text: str, max_tokens: int) -> str:
    """Return a prefix of ``text`` whose ``estimate_tokens`` ≤ ``max_tokens``.

    Iterates by code point (no surrogate-pair concern in Python; the
    same code-point iteration as TS but without the ``char.length``
    bookkeeping needed in JS). Empty / non-positive cap returns ``""``.
    """
    if max_tokens <= 0 or not text:
        return ""

    tokens = 0.0
    end = 0  # index into ``text`` by code point (= character in Python str)

    for char in text:
        next_tokens = tokens + _estimate_code_point_tokens(ord(char))
        if math.ceil(next_tokens) > max_tokens:
            break
        tokens = next_tokens
        end += 1

    return text[:end]
