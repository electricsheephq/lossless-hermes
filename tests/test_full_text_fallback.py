"""Tests for :mod:`lossless_hermes.store.full_text_fallback`.

Covers:

* :func:`contains_cjk` — CJK character detection.
* :func:`build_like_search_plan` — term extraction + de-dup + LIKE pattern build.
* :func:`create_fallback_snippet` — windowed snippet builder.

Source: ``/Volumes/LEXAR/Claude/lossless-claw/src/store/full-text-fallback.ts``
(LCM commit ``1f07fbd``).
"""

from __future__ import annotations

from lossless_hermes.store.full_text_fallback import (
    build_like_search_plan,
    contains_cjk,
    create_fallback_snippet,
)


# ---------------------------------------------------------------------------
# contains_cjk
# ---------------------------------------------------------------------------


def test_contains_cjk_detects_chinese() -> None:
    """Chinese characters return True."""
    assert contains_cjk("你好") is True


def test_contains_cjk_detects_japanese_hiragana() -> None:
    """Japanese Hiragana returns True."""
    assert contains_cjk("ひらがな") is True


def test_contains_cjk_detects_japanese_katakana() -> None:
    """Japanese Katakana returns True."""
    assert contains_cjk("カタカナ") is True


def test_contains_cjk_detects_korean_hangul() -> None:
    """Korean Hangul returns True."""
    assert contains_cjk("한글") is True


def test_contains_cjk_pure_latin_is_false() -> None:
    """Pure Latin text returns False."""
    assert contains_cjk("hello world") is False


def test_contains_cjk_mixed_content_is_true() -> None:
    """Any CJK character anywhere triggers True."""
    assert contains_cjk("hello 你好 world") is True


def test_contains_cjk_empty_string_is_false() -> None:
    """Empty input returns False."""
    assert contains_cjk("") is False


# ---------------------------------------------------------------------------
# build_like_search_plan
# ---------------------------------------------------------------------------


def test_build_like_plan_simple_query() -> None:
    """Simple multi-token query produces one LIKE clause per term."""
    plan = build_like_search_plan("content", "hello world")
    assert plan.terms == ["hello", "world"]
    assert plan.where == [
        "LOWER(content) LIKE ? ESCAPE '\\'",
        "LOWER(content) LIKE ? ESCAPE '\\'",
    ]
    assert plan.args == ["%hello%", "%world%"]


def test_build_like_plan_deduplicates() -> None:
    """Duplicate terms (case-insensitive) are de-duped in first-seen order."""
    plan = build_like_search_plan("content", "hello HELLO Hello")
    assert plan.terms == ["hello"]
    assert len(plan.where) == 1


def test_build_like_plan_strips_edge_punctuation() -> None:
    """Leading and trailing punctuation is stripped from terms."""
    plan = build_like_search_plan("content", "!!hello!! ...world,")
    assert plan.terms == ["hello", "world"]


def test_build_like_plan_quoted_phrase_preserved() -> None:
    """Double-quoted phrases become a single term."""
    plan = build_like_search_plan("content", '"error handling" debug')
    assert plan.terms == ["error handling", "debug"]


def test_build_like_plan_empty_query_returns_empty_terms() -> None:
    """Empty input returns no terms."""
    plan = build_like_search_plan("content", "")
    assert plan.terms == []
    assert plan.where == []
    assert plan.args == []


def test_build_like_plan_escapes_like_metacharacters() -> None:
    """SQL LIKE metacharacters ``%``, ``_``, ``\\`` are escaped."""
    plan = build_like_search_plan("content", r"50% off_now")
    # Edge punctuation strips trailing/leading, but the `%` and `_` remain.
    # 50% off_now is two tokens after splitting on whitespace.
    # The first token "50%" — trailing "%" is NOT in edge-punct.
    assert len(plan.args) == 2
    # Each arg has its metacharacters escaped.
    for arg in plan.args:
        # If the original contained "%", the escaped form should have "\%".
        # If it contained "_", it should have "\_".
        if "%" in arg and not arg.startswith(r"\%") and not arg.endswith(r"\%"):
            # The wrapping % at start and end are LIKE wildcards, not literal.
            inner = arg[1:-1]
            assert "%" not in inner or r"\%" in inner


# ---------------------------------------------------------------------------
# create_fallback_snippet
# ---------------------------------------------------------------------------


def test_create_fallback_snippet_with_match() -> None:
    """Snippet centers on the first matched term."""
    content = "The quick brown fox jumps over the lazy dog"
    snippet = create_fallback_snippet(content, ["fox"])
    assert "fox" in snippet


def test_create_fallback_snippet_no_match() -> None:
    """Without a match, returns head-of-content (truncated if long)."""
    content = "Short text"
    snippet = create_fallback_snippet(content, ["nomatch"])
    assert snippet == "Short text"


def test_create_fallback_snippet_no_match_long_truncates() -> None:
    """Long content with no match is truncated at 80 chars + ``...``."""
    content = "a" * 200
    snippet = create_fallback_snippet(content, ["nomatch"])
    assert len(snippet) <= 80
    assert snippet.endswith("...")


def test_create_fallback_snippet_match_near_start() -> None:
    """Match near start: no leading ``...`` marker."""
    content = "hello world"
    snippet = create_fallback_snippet(content, ["hello"])
    assert not snippet.startswith("...")


def test_create_fallback_snippet_match_in_middle() -> None:
    """Match in the middle of long content gets both leading + trailing ``...``."""
    content = "x" * 100 + " findme " + "y" * 100
    snippet = create_fallback_snippet(content, ["findme"])
    assert "findme" in snippet
    assert snippet.startswith("...")
    assert snippet.endswith("...")


def test_create_fallback_snippet_cjk_content() -> None:
    """CJK content snippet is built correctly (code-point slicing, no surrogate issues)."""
    content = "hello 你好 world"
    snippet = create_fallback_snippet(content, ["你好"])
    assert "你好" in snippet


def test_create_fallback_snippet_earliest_match_wins() -> None:
    """When multiple terms match, the earliest match is centered."""
    content = "alpha beta gamma delta beta epsilon"
    snippet = create_fallback_snippet(content, ["delta", "alpha"])
    # alpha appears first.
    assert "alpha" in snippet
