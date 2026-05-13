"""Build ``WHERE conversation_id IN (...)`` fragments.

Port of ``lossless-claw/src/store/conversation-scope.ts`` (LCM commit
``1f07fbd``). 34 LOC TS → Python.

The helper centralizes the "single conversation_id" vs. "multiple
conversation_ids" branching that appears across every search backend
(:func:`ConversationStore._search_full_text`,
:func:`ConversationStore._search_like`,
:func:`ConversationStore._search_regex`, and the analogous summary-store
paths). Without it, each backend would re-implement the same de-dup /
length-1 fast-path / parameterized IN-list pattern.
"""

from __future__ import annotations

from typing import Any, Iterable, List

__all__ = ["append_conversation_scope_constraint"]


def append_conversation_scope_constraint(
    *,
    where: List[str],
    args: List[Any],
    column_expr: str,
    conversation_id: int | None = None,
    conversation_ids: Iterable[int] | None = None,
) -> None:
    """Append a ``conversation_id``-scope predicate to a WHERE clause builder.

    Mirrors ``appendConversationScopeConstraint`` in TS verbatim. Mutates
    ``where`` (appends one SQL fragment) and ``args`` (appends the bind
    parameters) in place — no return value.

    Resolution order:

    1. If ``conversation_ids`` is non-empty (after de-dup + integer
       coercion + filter-out-NaN), use it; ignore ``conversation_id``.
    2. Otherwise if ``conversation_id`` is non-None, use it as a single-row
       filter.
    3. Otherwise add nothing.

    Args:
        where: List of SQL fragment strings (mutated in place).
        args: List of bind parameter values (mutated in place).
        column_expr: SQL expression for the conversation_id column, e.g.
            ``"m.conversation_id"`` or ``"conversation_id"``.
        conversation_id: Single-row filter (used only when
            ``conversation_ids`` is absent/empty).
        conversation_ids: Multi-row filter; values are de-duplicated and
            coerced to integers.

    Examples:
        Single-row filter via ``conversation_id``::

            where: list[str] = []
            args: list[Any] = []
            append_conversation_scope_constraint(
                where=where, args=args, column_expr="conversation_id",
                conversation_id=42,
            )
            # where == ["conversation_id = ?"]
            # args == [42]

        Multi-row filter via ``conversation_ids`` (length 1 fast-path)::

            append_conversation_scope_constraint(
                where=where, args=args, column_expr="m.conversation_id",
                conversation_ids=[7],
            )
            # where == ["m.conversation_id = ?"]
            # args == [7]

        Multi-row filter via ``conversation_ids`` (proper IN list)::

            append_conversation_scope_constraint(
                where=where, args=args, column_expr="m.conversation_id",
                conversation_ids=[7, 9, 11],
            )
            # where == ["m.conversation_id IN (?, ?, ?)"]
            # args == [7, 9, 11]
    """
    # Normalize conversation_ids: filter finite ints, de-dup preserving order.
    normalized: List[int] = []
    if conversation_ids is not None:
        seen: set[int] = set()
        for value in conversation_ids:
            if value is None:
                continue
            try:
                int_value = int(value)
            except (TypeError, ValueError):
                continue
            if int_value in seen:
                continue
            seen.add(int_value)
            normalized.append(int_value)

    if normalized:
        if len(normalized) == 1:
            where.append(f"{column_expr} = ?")
            args.append(normalized[0])
            return
        placeholders = ", ".join("?" for _ in normalized)
        where.append(f"{column_expr} IN ({placeholders})")
        args.extend(normalized)
        return

    if conversation_id is not None:
        where.append(f"{column_expr} = ?")
        args.append(conversation_id)
