"""Tests for :func:`lossless_hermes.store.conversation_scope.append_conversation_scope_constraint`.

Covers the three branches:

* Multi-id (length 1) — uses ``= ?`` fast-path.
* Multi-id (length ≥ 2) — uses ``IN (?, ?, ...)``.
* Single-id — uses ``= ?`` when ``conversation_ids`` is absent.

Plus de-duplication and the no-op branch.

Source: ``/Volumes/LEXAR/Claude/lossless-claw/src/store/conversation-scope.ts``
(LCM commit ``1f07fbd``).
"""

from __future__ import annotations

from typing import Any, List

from lossless_hermes.store.conversation_scope import (
    append_conversation_scope_constraint,
)


def test_single_conversation_id() -> None:
    """conversation_id only → ``= ?`` predicate."""
    where: List[str] = []
    args: List[Any] = []
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="conversation_id",
        conversation_id=42,
    )
    assert where == ["conversation_id = ?"]
    assert args == [42]


def test_single_conversation_id_with_table_alias() -> None:
    """The column_expr can be a qualified column with table alias."""
    where: List[str] = []
    args: List[Any] = []
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="m.conversation_id",
        conversation_id=7,
    )
    assert where == ["m.conversation_id = ?"]
    assert args == [7]


def test_conversation_ids_single_element_fast_path() -> None:
    """conversation_ids of length 1 uses ``= ?`` (not IN)."""
    where: List[str] = []
    args: List[Any] = []
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="conversation_id",
        conversation_ids=[5],
    )
    assert where == ["conversation_id = ?"]
    assert args == [5]


def test_conversation_ids_multiple() -> None:
    """conversation_ids of length ≥ 2 uses ``IN (?, ?, ...)``."""
    where: List[str] = []
    args: List[Any] = []
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="conversation_id",
        conversation_ids=[1, 2, 3],
    )
    assert where == ["conversation_id IN (?, ?, ?)"]
    assert args == [1, 2, 3]


def test_conversation_ids_deduplicates() -> None:
    """Duplicate values in conversation_ids are removed (first-seen order)."""
    where: List[str] = []
    args: List[Any] = []
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="conversation_id",
        conversation_ids=[5, 3, 5, 7, 3, 1],
    )
    assert where == ["conversation_id IN (?, ?, ?, ?)"]
    assert args == [5, 3, 7, 1]


def test_conversation_ids_preferred_over_conversation_id() -> None:
    """When both args are supplied, conversation_ids wins."""
    where: List[str] = []
    args: List[Any] = []
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="conversation_id",
        conversation_id=999,
        conversation_ids=[1, 2],
    )
    assert where == ["conversation_id IN (?, ?)"]
    assert args == [1, 2]


def test_no_args_is_noop() -> None:
    """No conversation_id and no conversation_ids → empty where/args."""
    where: List[str] = []
    args: List[Any] = []
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="conversation_id",
    )
    assert where == []
    assert args == []


def test_empty_conversation_ids_falls_through_to_id() -> None:
    """Empty conversation_ids → fall back to conversation_id."""
    where: List[str] = []
    args: List[Any] = []
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="conversation_id",
        conversation_id=42,
        conversation_ids=[],
    )
    assert where == ["conversation_id = ?"]
    assert args == [42]


def test_existing_where_args_are_preserved() -> None:
    """Existing where/args entries are not overwritten — only appended."""
    where: List[str] = ["created_at > ?"]
    args: List[Any] = ["2026-01-01"]
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="conversation_id",
        conversation_id=42,
    )
    assert where == ["created_at > ?", "conversation_id = ?"]
    assert args == ["2026-01-01", 42]


def test_conversation_ids_filters_invalid_values() -> None:
    """Non-integer convertible values in conversation_ids are dropped."""
    where: List[str] = []
    args: List[Any] = []
    append_conversation_scope_constraint(
        where=where,
        args=args,
        column_expr="conversation_id",
        conversation_ids=[1, "not_an_int", None, 2],  # type: ignore[list-item]
    )
    # "not_an_int" is dropped; None is dropped; 1 and 2 remain.
    assert where == ["conversation_id IN (?, ?)"]
    assert args == [1, 2]
