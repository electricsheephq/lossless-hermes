"""Tests for :mod:`lossless_hermes.estimate_tokens` (issue 03-01 / ADR-021).

Ports ``lossless-claw/test/estimate-tokens.test.ts`` (LCM commit
``1f07fbd``) fixture-for-fixture. The TS bench has 11 cases for
``estimateTokens``; this file mirrors each and adds:

* CJK parity sanity (``estimate_tokens("中文测试")`` agrees with the
  TS-side value to ±1 — required by issue 03-01 AC).
* Emoji ZWJ sequence (family emoji ``"👨‍👩‍👧‍👦"``).
* Combining-mark fixture: NFC ``"á"`` (one code point) vs NFD ``"á"``
  (two code points). Documents the cross-language behavior.
* :func:`truncate_text_to_estimated_tokens` boundary cases — exactly
  at ``max_tokens``, just over, just under, empty / non-positive cap.

The TS bench tests ASCII / CJK Han / Hiragana / Katakana / Hangul /
emoji / mixed / empty / CJK Extension B / fullwidth / CJK punctuation —
all 11 cases below.
"""

from __future__ import annotations

import unicodedata

import pytest

from lossless_hermes.estimate_tokens import (
    estimate_tokens,
    truncate_text_to_estimated_tokens,
)


# ---------------------------------------------------------------------------
# estimate_tokens: TS test-bench parity (11 cases, line-for-line port)
# ---------------------------------------------------------------------------


class TestEstimateTokensTsParity:
    """Each test mirrors one `it(...)` block in `estimate-tokens.test.ts`."""

    def test_ascii_text_at_quarter_tokens_per_char(self) -> None:
        # 11 chars × 0.25 = 2.75 → ceil → 3
        assert estimate_tokens("Hello world") == 3

    def test_cjk_han_at_1_5_tokens_per_char(self) -> None:
        # 4 chars × 1.5 = 6
        assert estimate_tokens("你好世界") == 6

    def test_hiragana_at_1_5_tokens_per_char(self) -> None:
        # 5 chars × 1.5 = 7.5 → ceil → 8
        assert estimate_tokens("こんにちは") == 8

    def test_katakana_at_1_5_tokens_per_char(self) -> None:
        # 4 chars × 1.5 = 6
        assert estimate_tokens("カタカナ") == 6

    def test_hangul_at_1_5_tokens_per_char(self) -> None:
        # 5 chars × 1.5 = 7.5 → ceil → 8
        assert estimate_tokens("안녕하세요") == 8

    def test_emoji_at_2_tokens_per_char(self) -> None:
        # 3 emoji × 2 = 6. Each emoji is a single code point in Python
        # (Python `for c in s` is code-point-aware; in TS each iter
        # produces the surrogate-pair code point too — same shape).
        assert estimate_tokens("🔥🎉💯") == 6

    def test_mixed_cjk_ascii_emoji(self) -> None:
        # "Hello 你好 🔥"
        # 5 ASCII (1.25) + space (0.25) + 2 Han (3) + space (0.25) +
        # emoji (2) = 6.75 → ceil → 7
        assert estimate_tokens("Hello 你好 🔥") == 7

    def test_empty_string_returns_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_cjk_extension_b_supplementary_plane(self) -> None:
        # 𠮷 (U+20BB7, CJK Extension B). CJK weighting wins → 1.5 → ceil → 2.
        assert estimate_tokens("𠮷") == 2

    def test_fullwidth_forms_at_1_5_tokens_per_char(self) -> None:
        # 3 fullwidth × 1.5 = 4.5 → ceil → 5
        assert estimate_tokens("ＡＢＣ") == 5

    def test_cjk_punctuation_at_1_5_tokens_per_char(self) -> None:
        # 3 chars × 1.5 = 4.5 → ceil → 5
        assert estimate_tokens("、。！") == 5


# ---------------------------------------------------------------------------
# Issue 03-01 AC: extra fixtures beyond the TS bench
# ---------------------------------------------------------------------------


class TestCjkParitySanity:
    """ADR-021 risk §Surrogate-pair / combining-mark parity.

    The TS reference value for ``estimateTokens("中文测试")`` is 6 (4 chars
    × 1.5). Asserting equality (±1 trivially holds when equal).
    """

    def test_chinese_test_fixture(self) -> None:
        # Direct parity assertion. Python and TS agree at the
        # estimate-tokens level on this fixture; the TS bench computes
        # 6 for `"你好世界"` (analogous shape).
        assert estimate_tokens("中文测试") == 6

    def test_long_chinese_paragraph(self) -> None:
        # 10 Han chars × 1.5 = 15
        assert estimate_tokens("一二三四五六七八九十") == 15


class TestEmojiZwjSequence:
    """ZWJ-joined family emoji.

    The family ``"👨‍👩‍👧‍👦"`` is **7 code points** (four people emoji + three
    U+200D zero-width joiners), not one. Both Python and TS iterate it
    as 7 code points. Each emoji (4 of them, all > U+FFFF) weights 2;
    each ZWJ (U+200D, in the BMP) is default-weighted 0.25.

    Expected: 4 × 2 + 3 × 0.25 = 8.75 → ceil → 9.
    """

    def test_family_zwj_emoji(self) -> None:
        assert estimate_tokens("👨‍👩‍👧‍👦") == 9

    def test_simple_zwj_man_woman(self) -> None:
        # "👨‍💻" = man + ZWJ + computer. 2 emoji × 2 + ZWJ × 0.25 = 4.25 → 5.
        assert estimate_tokens("👨‍💻") == 5


class TestCombiningMarkNfcVsNfd:
    """NFC vs NFD ``"á"`` parity check.

    * NFC ``"á"`` is one code point (U+00E1) — default-weighted 0.25 → 1.
    * NFD ``"á"`` is two code points (``"a"`` U+0061 + U+0301 combining
      acute) — both default-weighted, 0.5 → 1.

    Cross-language: both Python and TS see the same code-point count
    for each form (TS strings are UTF-16 but ASCII / combining marks are
    BMP one-unit-each, so the iteration counts agree). Documenting both
    forms here ensures any future estimator change keeps the contract
    that *the estimator does NOT normalize input*. Caller-side
    normalization (if needed) is out of scope.
    """

    def test_nfc_a_acute_is_one_code_point(self) -> None:
        nfc = unicodedata.normalize("NFC", "á")
        assert len(nfc) == 1
        # 1 char × 0.25 = 0.25 → ceil → 1
        assert estimate_tokens(nfc) == 1

    def test_nfd_a_acute_is_two_code_points(self) -> None:
        nfd = unicodedata.normalize("NFD", "á")
        assert len(nfd) == 2  # "a" + U+0301 combining acute
        # 2 chars × 0.25 = 0.5 → ceil → 1
        assert estimate_tokens(nfd) == 1

    def test_nfc_vs_nfd_token_estimate_can_match(self) -> None:
        """Single-char `á` happens to round to the same value in both
        forms (0.25 vs 0.5, both ceil to 1). Longer combining-heavy
        strings can diverge — documented here as expected behavior."""
        nfc = unicodedata.normalize("NFC", "á")
        nfd = unicodedata.normalize("NFD", "á")
        assert estimate_tokens(nfc) == estimate_tokens(nfd) == 1


class TestAsciiParagraph:
    """~500-char ASCII paragraph (issue AC: long-ASCII coverage)."""

    def test_500_char_paragraph(self) -> None:
        text = "The quick brown fox jumps over the lazy dog. " * 11
        # 11 × 45 = 495 chars; 495 × 0.25 = 123.75 → ceil → 124
        assert len(text) == 495
        assert estimate_tokens(text) == 124


# ---------------------------------------------------------------------------
# truncate_text_to_estimated_tokens
# ---------------------------------------------------------------------------


class TestTruncate:
    """Tests for :func:`truncate_text_to_estimated_tokens`.

    Invariant: ``estimate_tokens(truncate(text, n)) <= n`` for every
    text + non-negative n.
    """

    @pytest.mark.parametrize("max_tokens", [0, -1, -100])
    def test_non_positive_cap_returns_empty(self, max_tokens: int) -> None:
        assert truncate_text_to_estimated_tokens("Hello world", max_tokens) == ""

    def test_empty_input_returns_empty(self) -> None:
        assert truncate_text_to_estimated_tokens("", 100) == ""

    def test_ascii_under_cap_keeps_all(self) -> None:
        # 11 chars × 0.25 = 2.75 → 3 tokens; cap 10 is plenty.
        assert truncate_text_to_estimated_tokens("Hello world", 10) == "Hello world"

    def test_ascii_just_at_cap_keeps_all(self) -> None:
        # "Hello world" → 3 tokens. Cap exactly 3 keeps the full string.
        result = truncate_text_to_estimated_tokens("Hello world", 3)
        assert result == "Hello world"
        assert estimate_tokens(result) <= 3

    def test_ascii_just_below_cap_truncates(self) -> None:
        # 11 chars total. cap=2 means we can only afford ceil(x*0.25) <= 2
        # which is x <= 8 chars (8 * 0.25 = 2.0, exact; 9 * 0.25 = 2.25 → 3).
        result = truncate_text_to_estimated_tokens("Hello world", 2)
        assert estimate_tokens(result) <= 2
        # The algorithm should keep as many chars as the cap allows.
        assert result == "Hello wo"

    def test_cjk_truncate_at_boundary(self) -> None:
        # "你好世界" → 4 × 1.5 = 6 tokens. Cap=4 means we can fit at most
        # 2 chars (2 * 1.5 = 3 → 3 tokens), but 3 chars = 4.5 → 5 > 4.
        result = truncate_text_to_estimated_tokens("你好世界", 4)
        assert estimate_tokens(result) <= 4
        assert result == "你好"

    def test_cjk_truncate_exact_full_cap(self) -> None:
        # cap exactly the total — keeps everything.
        assert truncate_text_to_estimated_tokens("你好世界", 6) == "你好世界"

    def test_emoji_truncate_no_surrogate_split(self) -> None:
        # 3 emoji × 2 = 6 tokens. Cap=3 keeps 1 emoji (2 tokens),
        # cap=4 keeps 2 (4 tokens), cap=5 keeps 2 (cap=6 would be 3).
        assert truncate_text_to_estimated_tokens("🔥🎉💯", 3) == "🔥"
        assert truncate_text_to_estimated_tokens("🔥🎉💯", 4) == "🔥🎉"
        assert truncate_text_to_estimated_tokens("🔥🎉💯", 5) == "🔥🎉"
        assert truncate_text_to_estimated_tokens("🔥🎉💯", 6) == "🔥🎉💯"

    def test_truncate_just_over_threshold(self) -> None:
        # 5 chars ASCII = 1.25 → 2 tokens. Cap=1 keeps 4 chars
        # (4 × 0.25 = 1.0 exact, ceil → 1). 5th char would push to 1.25 → 2.
        result = truncate_text_to_estimated_tokens("Hello", 1)
        assert estimate_tokens(result) <= 1
        assert result == "Hell"

    def test_truncate_invariant_holds(self) -> None:
        """Property: estimate_tokens(truncate(t, n)) <= n for various inputs."""
        cases = [
            ("Hello world", 0),
            ("Hello world", 1),
            ("Hello world", 2),
            ("Hello world", 3),
            ("Hello world", 100),
            ("你好世界", 0),
            ("你好世界", 1),
            ("你好世界", 2),
            ("你好世界", 6),
            ("🔥🎉💯", 1),
            ("🔥🎉💯", 4),
            ("Mixed: 你好 🔥 world", 5),
            ("", 100),
        ]
        for text, cap in cases:
            result = truncate_text_to_estimated_tokens(text, cap)
            est = estimate_tokens(result)
            assert est <= cap, f"truncate({text!r}, {cap}) = {result!r} has est={est} > cap={cap}"


# ---------------------------------------------------------------------------
# Module-shape sanity: public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_estimate_tokens_is_callable(self) -> None:
        from lossless_hermes import estimate_tokens as module

        assert callable(module.estimate_tokens)
        assert callable(module.truncate_text_to_estimated_tokens)

    def test_returns_int_not_float(self) -> None:
        # estimate_tokens returns an int (math.ceil), not a float.
        result = estimate_tokens("Hello")
        assert isinstance(result, int)

    def test_truncate_returns_str(self) -> None:
        result = truncate_text_to_estimated_tokens("Hello", 10)
        assert isinstance(result, str)
