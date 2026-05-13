"""Resolve "current conversation" vs "all conversations" scope for tools.

Port of ``lossless-claw/src/tools/lcm-conversation-scope.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 162 LOC TS → ~190 LOC Python).

Two public exports — both consumed by every LCM tool that accepts a
``conversationId`` / ``allConversations`` / ``since`` / ``before``
parameter (``lcm_grep``, ``lcm_describe``, ``lcm_search_entities``,
``lcm_expand``, ``lcm_synthesize_around`` — i.e. every tool except
``lcm_compact``):

* :func:`parse_iso_timestamp_param` — defensive parser for ISO 8601 /
  RFC 3339 timestamps from a string-keyed params dict. Returns ``None``
  on missing/empty input; raises :class:`ValueError` on garbage (the
  caller wraps that into a tool-error response).
* :func:`resolve_lcm_conversation_scope` — the priority ladder that
  decides which conversation(s) a tool call targets.

The TS source ``async`` markings are dropped per ADR-017
(synchronous-by-design): every ``ConversationStore`` call is a plain
``def`` over :class:`sqlite3.Connection`.

### Resolution priority (lines 92–161 of the TS source)

1. Explicit ``params["conversationId"]`` (a number) →
   ``{conversation_id, conversation_ids: [it], all_conversations: False}``.
2. ``params.get("allConversations") is True`` →
   ``{all_conversations: True}`` (no IDs — the caller scans every row).
3. ``session_key`` lookup via
   :meth:`ConversationStore.get_conversation_by_session_key` →
   family-expand via :meth:`ConversationStore.get_conversation_family_ids`
   for cross-conversation scoping within the session family.
4. Fall through to ``session_id`` lookup. If no ``session_id`` was
   passed, try ``deps.resolve_session_id_from_session_key`` (a callback
   that maps a session-key string to a runtime session id — the
   :class:`LcmDependencies` injection seam).
5. If nothing matches, return the empty
   ``{all_conversations: False, conversation_id: None}`` scope.

### Family-expansion SQL

The family-expand uses ``ConversationStore.get_conversation_family_ids``
which itself executes ``SELECT conversation_id FROM conversations WHERE
session_key = ?`` (or ``session_id`` fallback) — equivalent to the TS
recursive-CTE / parent walk for session-family scoping (see
``docs/porting-guides/tools.md`` line 559 reference to
``WHERE root_conversation_id = ?``; the Python port uses session_key/id
matching because that's the actual identity surface in the v4.1 schema).

### Resolution dependencies

The function takes the LCM engine via a structural :class:`_LcmLike`
:class:`~typing.Protocol` to dodge the engine ↔ tools import cycle.
Anything exposing a ``_conversation_store`` attribute of type
:class:`~lossless_hermes.store.conversation.ConversationStore` satisfies
the contract.

See:

* TS source: ``/Volumes/LEXAR/Claude/lossless-claw/src/tools/lcm-conversation-scope.ts``
* Issue spec: ``epics/06-tools/06-05-conversation-scope.md``
* ADR-017 — synchronous-by-design.
* :mod:`lossless_hermes.store.conversation` — the ConversationStore that
  ``get_conversation_by_session_key`` + ``get_conversation_family_ids``
  live on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Protocol

from lossless_hermes.store.conversation import ConversationStore

__all__ = [
    "LcmConversationScope",
    "LcmDependencies",
    "parse_iso_timestamp_param",
    "resolve_lcm_conversation_scope",
]


# ---------------------------------------------------------------------------
# Public dataclasses / structural types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LcmConversationScope:
    """Resolved scope: which conversation(s) a tool call targets.

    Mirrors the TS ``LcmConversationScope`` type (lines 4-8 of the
    source). The three fields are mutually constrained:

    * ``all_conversations = True`` → both ``conversation_id`` and
      ``conversation_ids`` are ``None`` (caller scans every row).
    * Explicit-id branch (param ``conversationId``) →
      ``conversation_ids == [conversation_id]``.
    * Session-family branch → ``conversation_ids`` may have multiple
      entries (parent + children) and ``conversation_id`` is the
      "anchor" (the row resolved from session_key / session_id).
    * No match → ``all_conversations = False`` AND
      ``conversation_id is None`` AND ``conversation_ids is None``.

    Attributes:
        conversation_id: The single "anchor" conversation id, or
            ``None`` when the scope is all-conversations / no-match.
        conversation_ids: The full list of conversation ids in the
            target scope (1 entry for explicit-id, N for family
            expansion, ``None`` for all-conversations / no-match).
        all_conversations: ``True`` when the caller explicitly opted
            into cross-conversation mode.
    """

    conversation_id: Optional[int] = None
    conversation_ids: Optional[list[int]] = None
    all_conversations: bool = False


@dataclass(frozen=True)
class LcmDependencies:
    """Minimal dependency-injection slice consumed by scope resolution.

    Narrow port of the TS ``LcmDependencies`` interface — only the
    ``resolve_session_id_from_session_key`` callback is consulted here.
    Other LCM tools will widen this dataclass with their own slices as
    they port; nothing forces a single monolithic dependencies object.

    The callback is optional: when ``None`` (i.e. ``deps is None`` at
    the call site) the resolver simply skips step 4's session-key →
    session-id fallback. This matches the TS behavior where ``deps`` is
    declared optional in the function signature (line 90).

    Attributes:
        resolve_session_id_from_session_key: Callback that maps a
            session-key string to a runtime session-id string, or
            ``None`` if the key cannot be resolved. Matches TS
            ``resolveSessionIdFromSessionKey: (sessionKey: string) =>
            Promise<string | undefined>``. The Python port is sync per
            ADR-017.
    """

    resolve_session_id_from_session_key: Callable[[str], Optional[str]]


class _LcmLike(Protocol):
    """Structural shape consumed by :func:`resolve_lcm_conversation_scope`.

    The function only needs the ``_conversation_store`` attribute; using
    a :class:`~typing.Protocol` dodges the engine ↔ tools import cycle
    (the engine package will eventually import this module via the tool
    dispatch table in issue 06-02).

    Anything with a ``_conversation_store`` of type
    :class:`~lossless_hermes.store.conversation.ConversationStore`
    satisfies the contract — production calls supply the real
    :class:`~lossless_hermes.engine.LCMEngine`; tests can supply a tiny
    stand-in with ``_conversation_store = <ConversationStore instance>``.
    """

    _conversation_store: Optional[ConversationStore]


# ---------------------------------------------------------------------------
# parse_iso_timestamp_param
# ---------------------------------------------------------------------------


def parse_iso_timestamp_param(
    params: Mapping[str, Any],
    key: str,
) -> Optional[datetime]:
    """Parse an ISO-8601 / RFC 3339 timestamp from a tool params dict.

    Mirrors TS ``parseIsoTimestampParam`` (lines 58-75 of the source).
    Returns ``None`` when the key is absent, the value is not a string,
    or the trimmed string is empty. Raises :class:`ValueError` on a
    non-empty string that is not a parseable ISO timestamp.

    Implementation notes:

    * **Type-check first.** Non-string values (including ``None``,
      numbers, dicts) silently return ``None`` — matching the TS
      ``typeof raw !== "string"`` guard. This is intentional: tool
      params come from JSON and the caller's schema validator (TypeBox
      in TS, pydantic in Python) is the loud-failure surface. This
      helper is the defensive last-mile.
    * **Trim whitespace.** Both leading and trailing whitespace are
      stripped before parsing. An all-whitespace value normalizes to
      empty → ``None``.
    * **Z suffix handling.** Python 3.11+'s
      :meth:`datetime.fromisoformat` accepts the trailing ``Z``
      directly. The project targets Python 3.11/3.12/3.13 per
      ``pyproject.toml``, so no explicit ``Z → +00:00`` translation is
      needed.
    * **Error shape.** The TS source throws
      ``new Error("${key} must be a valid ISO timestamp.")`` (a template
      literal in JS) — we raise :class:`ValueError` with the same
      message format. The surrounding tool handler is the layer that
      catches this and shapes it into the structured tool-error JSON
      response.

    Args:
        params: The tool params dict. Read-only — never mutated.
        key: The param name to look up. Used both for the dict lookup
            and the error message.

    Returns:
        A :class:`datetime` parsed from the trimmed string value, or
        ``None`` if absent/empty/non-string.

    Raises:
        ValueError: If the value is a non-empty string that is not a
            parseable ISO timestamp.
    """
    raw = params.get(key)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be a valid ISO timestamp.") from exc


# ---------------------------------------------------------------------------
# resolve_lcm_conversation_scope
# ---------------------------------------------------------------------------


def _lookup_conversation_for_session(
    *,
    store: ConversationStore,
    session_id: Optional[str],
    session_key: Optional[str],
) -> Optional[int]:
    """Resolve the active conversation id from session_id / session_key.

    Ports the TS ``lookupConversationForSession`` helper (lines 23-51).
    The Python :class:`ConversationStore` exposes a single
    :meth:`~ConversationStore.get_conversation_for_session` that
    encapsulates the session_key → session_id fallback already, so we
    delegate to it (rather than re-implementing the TS branching with
    optional method-presence probes).

    Args:
        store: The conversation store to query.
        session_id: Optional runtime session identifier.
        session_key: Optional cross-conversation session-family key.

    Returns:
        The matching ``conversation_id`` integer, or ``None`` if no
        active conversation matches either key.
    """
    record = store.get_conversation_for_session(
        session_id=session_id,
        session_key=session_key,
    )
    return record.conversation_id if record is not None else None


def resolve_lcm_conversation_scope(
    *,
    lcm: _LcmLike,
    params: Mapping[str, Any],
    session_id: Optional[str] = None,
    session_key: Optional[str] = None,
    deps: Optional[LcmDependencies] = None,
) -> LcmConversationScope:
    """Resolve which conversation(s) a tool call targets.

    Mirrors TS ``resolveLcmConversationScope`` (lines 85-162 of the
    source). Resolution priority:

    1. Explicit ``params["conversationId"]`` (int or finite-float) →
       single-conversation scope.
    2. ``params.get("allConversations") is True`` → cross-conversation
       (no IDs).
    3. ``session_key`` lookup via the store → session-family expansion.
    4. ``session_id`` lookup (with optional ``deps`` fallback) →
       single-anchor + family-expand.
    5. Otherwise → empty scope.

    Args:
        lcm: The engine (or any object exposing
            ``_conversation_store``). The ConversationStore is the only
            thing this function consumes off the engine.
        params: Tool params dict (typically the JSON tool call args).
            Read-only — never mutated.
        session_id: Optional runtime session id passed by the caller
            (e.g. the gateway dispatch hook).
        session_key: Optional cross-conversation session-family key.
        deps: Optional :class:`LcmDependencies` slice. When present and
            ``session_id`` is missing but ``session_key`` is provided,
            ``deps.resolve_session_id_from_session_key`` is consulted
            to derive a session_id.

    Returns:
        A :class:`LcmConversationScope` populated per the priority
        ladder above.

    Raises:
        RuntimeError: If the engine has no conversation store wired
            (i.e. ``on_session_start`` did not run before the tool
            dispatch). This is a programmer error; production callers
            never see it.
    """
    store = lcm._conversation_store
    if store is None:
        raise RuntimeError(
            "resolve_lcm_conversation_scope: lcm._conversation_store is None "
            "(on_session_start did not run?)"
        )

    # ----- Priority 1: explicit conversationId param ------------------------
    # TS uses ``typeof params.conversationId === "number" &&
    # Number.isFinite(params.conversationId)``. The Python equivalent
    # accepts ``int`` directly and falls through ``bool`` (a subclass of
    # int — ``isinstance(True, int)`` is ``True``, and we don't want
    # ``allConversations=True`` accidentally read as ``conversationId=1``).
    # Floats are coerced via ``int()`` for parity with TS's ``Math.trunc``,
    # but NaN/Inf are rejected (TS ``Number.isFinite`` parity). The
    # ``bool`` exclusion is the Python-specific guard.
    explicit_id = params.get("conversationId")
    if isinstance(explicit_id, bool):
        # ``True`` / ``False`` are int subclasses in Python; reject them
        # explicitly so a misuse like ``{"conversationId": True}`` doesn't
        # truncate to id=1.
        explicit_id = None
    if isinstance(explicit_id, int):
        truncated = int(explicit_id)
        return LcmConversationScope(
            conversation_id=truncated,
            conversation_ids=[truncated],
            all_conversations=False,
        )
    if isinstance(explicit_id, float):
        # Mirror TS ``Number.isFinite`` rejection of NaN/+-Inf, then
        # ``Math.trunc`` toward zero.
        if explicit_id != explicit_id or explicit_id in (float("inf"), float("-inf")):
            pass  # fall through to next priority
        else:
            truncated = int(explicit_id)  # truncates toward zero
            return LcmConversationScope(
                conversation_id=truncated,
                conversation_ids=[truncated],
                all_conversations=False,
            )

    # ----- Priority 2: allConversations=True --------------------------------
    if params.get("allConversations") is True:
        return LcmConversationScope(
            conversation_id=None,
            conversation_ids=None,
            all_conversations=True,
        )

    # ----- Priority 3: session_key path -------------------------------------
    normalized_session_key = session_key.strip() if isinstance(session_key, str) else ""
    if normalized_session_key:
        by_session_key = store.get_conversation_by_session_key(normalized_session_key)
        if by_session_key is not None:
            family_ids = store.get_conversation_family_ids(
                conversation_id=by_session_key.conversation_id,
                session_key=normalized_session_key,
            )
            return LcmConversationScope(
                conversation_id=by_session_key.conversation_id,
                conversation_ids=family_ids if family_ids else [by_session_key.conversation_id],
                all_conversations=False,
            )

    # ----- Priority 4: session_id path (with deps fallback) -----------------
    normalized_session_id = session_id.strip() if isinstance(session_id, str) else ""
    if not normalized_session_id and normalized_session_key and deps is not None:
        # The deps callback may return ``None`` — fall through to the
        # empty-scope branch below if so.
        resolved = deps.resolve_session_id_from_session_key(normalized_session_key)
        if resolved:
            normalized_session_id = resolved.strip() if isinstance(resolved, str) else ""

    if not normalized_session_id and not normalized_session_key:
        return LcmConversationScope(
            conversation_id=None,
            conversation_ids=None,
            all_conversations=False,
        )

    conversation_id = _lookup_conversation_for_session(
        store=store,
        session_id=normalized_session_id or None,
        session_key=normalized_session_key or None,
    )
    if conversation_id is None:
        return LcmConversationScope(
            conversation_id=None,
            conversation_ids=None,
            all_conversations=False,
        )

    family_ids = store.get_conversation_family_ids(
        conversation_id=conversation_id,
        session_id=normalized_session_id or None,
        session_key=normalized_session_key or None,
    )
    return LcmConversationScope(
        conversation_id=conversation_id,
        conversation_ids=family_ids if family_ids else [conversation_id],
        all_conversations=False,
    )
