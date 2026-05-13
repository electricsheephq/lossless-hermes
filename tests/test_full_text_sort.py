"""Tests for :mod:`lossless_hermes.store.full_text_sort`.

Mirrors the three sort modes documented in
``lossless-claw/src/store/full-text-sort.ts``.
"""

from __future__ import annotations

from lossless_hermes.store.full_text_sort import AGE_DECAY_RATE, build_fts_order_by


def test_default_is_recency() -> None:
    """When sort is None, default is ``recency``."""
    expr = build_fts_order_by(None, "m.created_at")
    assert expr == "m.created_at DESC"


def test_recency_mode() -> None:
    """Recency mode: pure created_at DESC."""
    assert build_fts_order_by("recency", "m.created_at") == "m.created_at DESC"


def test_relevance_mode() -> None:
    """Relevance mode: rank ASC, created_at DESC as tiebreaker."""
    expr = build_fts_order_by("relevance", "m.created_at")
    assert expr == "rank ASC, m.created_at DESC"


def test_hybrid_mode_contains_age_decay() -> None:
    """Hybrid mode uses the AGE_DECAY_RATE constant in its formula."""
    expr = build_fts_order_by("hybrid", "m.created_at")
    assert str(AGE_DECAY_RATE) in expr
    assert "julianday('now')" in expr
    assert "julianday(m.created_at)" in expr


def test_works_with_table_alias_or_plain_column() -> None:
    """The created_at_expr can be a qualified column or plain name."""
    assert build_fts_order_by("recency", "created_at") == "created_at DESC"
    assert build_fts_order_by("recency", "m.created_at") == "m.created_at DESC"
