"""Build the optional ``WHERE conversation_id IN (...)`` fragment for searches.

Ports ``lossless-claw/src/store/conversation-scope.ts`` (LCM commit ``1f07fbd``,
34 LOC TS → ~40 LOC Python). Used by every search path in
:mod:`lossless_hermes.store.summary` (FTS5, LIKE, CJK trigram, CJK LIKE, regex)
to optionally narrow the query to one or many conversations.

The single-conv case (``conversation_id`` scalar) is the hot path; the
many-conv case (``conversation_ids`` list) is used by cross-conversation
retrieval features added in v4.1. When neither is supplied, no constraint is
appended — searches span the whole DB.

See:

* ``/Volumes/LEXAR/Claude/lossless-claw/src/store/conversation-scope.ts`` —
  TS canonical (commit ``1f07fbd``).
* ``docs/porting-guides/storage.md`` §4.2 row "conversation-scope.ts".
"""

from __future__ import annotations

from typing import Any


def append_conversation_scope_constraint(
    *,
    where: list[str],
    args: list[Any],
    column_expr: str,
    conversation_id: int | None = None,
    conversation_ids: list[int] | None = None,
) -> None:
    """Append a ``WHERE`` fragment that scopes the search to one or many convs.

    Mirrors TS ``appendConversationScopeConstraint``. Mutates ``where`` and
    ``args`` in place — chosen to match the TS call sites that thread a single
    pair of accumulators through several builders.

    Resolution order:

    1. ``conversation_ids`` (if non-empty) wins. Single-element lists collapse
       to ``= ?``; multi-element lists become ``IN (?, ?, ...)``.
    2. Falls back to ``conversation_id`` (the scalar path) when
       ``conversation_ids`` is empty/missing.
    3. No constraint appended when both are ``None``/empty — caller's query
       runs DB-wide.

    Args:
        where: Mutable list of ``WHERE`` clauses; new fragment(s) appended.
        args: Mutable list of bound-parameter values; new value(s) appended in
            the same order as the ``WHERE`` fragments.
        column_expr: Fully-qualified column reference (e.g. ``"s.conversation_id"``
            or ``"conversation_id"``) — depends on whether the caller's query
            uses a table alias.
        conversation_id: Single conversation scope. Used when
            ``conversation_ids`` is None or empty.
        conversation_ids: Many-conversation scope. Empty list ≡ ``None``.
            Duplicates and non-integer values are filtered out — the TS source
            uses ``new Set(...)`` + ``Math.trunc``; we mirror via dict-from-keys
            (preserves insertion order) + ``int()`` coercion.
    """
    # Filter + dedupe (preserving insertion order, like a TS Set).
    if conversation_ids:
        normalized_ids: list[int] = []
        seen: set[int] = set()
        for value in conversation_ids:
            if not isinstance(value, (int, float)):
                continue
            # ``Math.trunc`` rounds toward zero for floats. Python's ``int()``
            # does the same on floats.
            truncated = int(value)
            if truncated not in seen:
                seen.add(truncated)
                normalized_ids.append(truncated)
    else:
        normalized_ids = []

    if normalized_ids:
        if len(normalized_ids) == 1:
            where.append(f"{column_expr} = ?")
            args.append(normalized_ids[0])
            return

        placeholders = ", ".join("?" for _ in normalized_ids)
        where.append(f"{column_expr} IN ({placeholders})")
        args.extend(normalized_ids)
        return

    if conversation_id is not None:
        where.append(f"{column_expr} = ?")
        args.append(conversation_id)
