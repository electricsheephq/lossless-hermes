"""Tests for :func:`lossless_hermes.store.fts5_sanitize.sanitize_fts5_query`.

Ports the 16 cases from ``lossless-claw/test/fts5-sanitize.test.ts``
verbatim. The TS test uses ``expect().toBe(...)`` for exact string
equality; we use ``==`` in pytest.

Source: ``/Volumes/LEXAR/Claude/lossless-claw/test/fts5-sanitize.test.ts``
(LCM commit ``1f07fbd``).
"""

from __future__ import annotations

from lossless_hermes.store.fts5_sanitize import sanitize_fts5_query


def test_quotes_simple_tokens() -> None:
    """Tokens separated by whitespace each get wrapped in double quotes."""
    assert sanitize_fts5_query("hello world") == '"hello" "world"'


def test_preserves_hyphens_inside_quotes() -> None:
    """The ``-`` operator is neutralized by wrapping in quotes."""
    assert sanitize_fts5_query("sub-agent restrict") == '"sub-agent" "restrict"'


def test_neutralizes_boolean_operators() -> None:
    """``OR`` becomes a literal token, not the FTS5 operator."""
    assert sanitize_fts5_query("lcm_expand OR crash") == '"lcm_expand" "OR" "crash"'


def test_strips_internal_double_quotes() -> None:
    """Internal double quotes are stripped before wrapping."""
    assert sanitize_fts5_query('hello "world"') == '"hello" "world"'


def test_handles_colons_column_filter_syntax() -> None:
    """The ``:`` column filter syntax is neutralized."""
    assert sanitize_fts5_query("agent:foo bar") == '"agent:foo" "bar"'


def test_handles_prefix_star_operator() -> None:
    """The ``*`` prefix operator is preserved inside quotes (no-op)."""
    assert sanitize_fts5_query("lcm*") == '"lcm*"'


def test_handles_empty_string() -> None:
    """Empty input returns the literal empty phrase."""
    assert sanitize_fts5_query("") == '""'


def test_handles_whitespace_only() -> None:
    """Whitespace-only input returns the empty phrase."""
    assert sanitize_fts5_query("   ") == '""'


def test_handles_single_token() -> None:
    """A single token is wrapped in quotes."""
    assert sanitize_fts5_query("expand") == '"expand"'


def test_collapses_multiple_spaces() -> None:
    """Multiple whitespace characters between tokens are collapsed."""
    assert sanitize_fts5_query("a   b   c") == '"a" "b" "c"'


def test_handles_NOT_operator() -> None:
    """``NOT`` becomes a literal token."""
    assert sanitize_fts5_query("NOT agent") == '"NOT" "agent"'


def test_handles_NEAR_operator() -> None:
    """``NEAR(a b)`` is tokenized as two parenthesized tokens."""
    assert sanitize_fts5_query("NEAR(a b)") == '"NEAR(a" "b)"'


def test_handles_caret_initial_token() -> None:
    """The ``^`` initial-token operator is preserved inside quotes."""
    assert sanitize_fts5_query("^start") == '"^start"'


def test_preserves_multi_word_quoted_phrases() -> None:
    """User-quoted ``"error handling"`` is preserved as a phrase."""
    assert sanitize_fts5_query('"error handling" debug') == '"error handling" "debug"'


def test_preserves_multiple_quoted_phrases() -> None:
    """Multiple ``"..."`` phrases are all preserved."""
    assert (
        sanitize_fts5_query('"error handling" OR "crash report"')
        == '"error handling" "OR" "crash report"'
    )


def test_handles_mixed_quoted_and_unquoted_terms() -> None:
    """Unquoted tokens around a quoted phrase are all wrapped."""
    assert (
        sanitize_fts5_query('find "database migration" in code')
        == '"find" "database migration" "in" "code"'
    )


def test_handles_empty_quoted_phrase() -> None:
    """An empty ``""`` quoted phrase is treated as no-tokens → empty phrase."""
    assert sanitize_fts5_query('""') == '""'
