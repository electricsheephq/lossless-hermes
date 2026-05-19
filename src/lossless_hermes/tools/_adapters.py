"""Dispatch adapters — bridge the engine-uniform call shape to typed handlers.

Issue [#156](https://github.com/electricsheephq/lossless-hermes/issues/156)
is the P0 where the ported ``lcm_*`` tools are advertised in
``TOOL_SCHEMAS`` (so the model *sees* them) but absent from
``TOOL_DISPATCH`` (so they can never *run*). Epic 06's per-tool issues
delivered the handler bodies + schemas + unit tests but never the
**dispatch-adapter layer** — the per-tool typed-context construction and
``TOOL_DISPATCH`` registration.

The ported handlers cannot be registered directly. ``_dispatch_tool_call``
invokes every registered callable as ``handler(args, **kwargs)`` where
``kwargs`` carries ``runtime_ctx``, ``session_key``, ``ctx=<engine>`` and
(sometimes) ``messages``. But the ported handlers declare strict
keyword-only typed signatures — e.g. ``handle_lcm_grep(args, *, ctx:
GrepContext, deps: LcmDependencies, session_key=None, session_id=None)`` —
with no ``runtime_ctx`` parameter, no ``**kwargs`` sink, and a ``ctx``
typed as a narrow per-tool ``*Context`` Protocol rather than the engine.
A naive ``TOOL_DISPATCH["lcm_grep"] = handle_lcm_grep`` would ``TypeError``
on the first dispatch.

This module is the fix. For each ported tool there is an adapter
``_adapt_lcm_<tool>(args, **kwargs) -> str`` carrying the uniform
dispatch signature. Each adapter:

1. reads the engine off ``kwargs["ctx"]`` (``_dispatch_tool_call``
   injects the engine there via ``kwargs.setdefault("ctx", self)``);
2. builds the tool's typed ``*Context`` — a frozen :func:`dataclass`
   that structurally satisfies the handler's ``*Context`` Protocol;
3. builds ``deps`` (a :class:`LcmDependencies`) when the handler needs
   one;
4. calls the real ``handle_lcm_<tool>`` with the correct keyword args;
5. returns its JSON string verbatim.

The PR-1 adapters wire four tools: ``lcm_get_entity``,
``lcm_search_entities``, ``lcm_describe``, ``lcm_grep``. ``lcm_compact``
and ``lcm_synthesize_around`` ship in #156 PR-2 / PR-3; ``lcm_expand`` is
deferred per ADR-037.

Engine → context member mapping
-------------------------------

The engine exposes private, ``Optional`` collaborators (``None`` until
``on_session_start`` runs). The adapters translate them to the
Protocols' public, non-optional shape:

* ``conn`` ← ``engine._db``
* ``summary_store`` ← ``engine._summary_store``
* ``conversation_store`` ← ``engine._conversation_store``
* ``timezone`` ← ``engine.config.timezone``
* ``embeddings_enabled`` ← ``engine.config.embeddings_enabled``
* ``max_expand_tokens`` ← ``engine.config.max_expand_tokens``
* ``voyage`` ← ``None`` (ADR-033: embeddings are opt-in / off by
  default; ``lcm_grep``'s regex / full_text / verbatim modes never
  touch Voyage, and hybrid / semantic refuse cleanly when it is
  ``None``)

Engine-state timing
-------------------

``engine._db`` / ``engine._summary_store`` / ``engine._conversation_store``
are ``None`` until ``on_session_start``. A tool dispatch always happens
inside an active session, so they are populated at call time — but each
adapter degrades gracefully (returns a structured ``tool_result``
error, not an :class:`AttributeError`) if engine state is unset. PR-0's
crash-hardening in ``_dispatch_tool_call`` is a backstop; this explicit
guard is the belt.

References
----------

* Issue #156 — the P0 and its four-PR dispatch-adapter plan; §7 of the
  scoping-plan comment is the adapter-pattern spec.
* ADR-033 (``docs/adr/033-embeddings-opt-in.md``) — embeddings opt-in /
  off by default; the ``voyage=None`` rationale.
* ADR-037 (``docs/adr/037-lcm-expand-deferred.md``) — ``lcm_expand``
  deferral.
* ``src/lossless_hermes/engine/__init__.py`` — ``TOOL_DISPATCH`` and the
  ``_dispatch_tool_call`` seam the adapters register into.
* ``tests/test_dispatch_registry_coverage.py`` — the #156 regression
  ratchet these adapters flip green.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.store.summary import SummaryStore
from lossless_hermes.tools._common import tool_result
from lossless_hermes.tools.conversation_scope import LcmDependencies
from lossless_hermes.tools.describe import handle_lcm_describe
from lossless_hermes.tools.get_entity import handle_lcm_get_entity
from lossless_hermes.tools.grep import handle_lcm_grep
from lossless_hermes.tools.search_entities import handle_lcm_search_entities
from lossless_hermes.voyage.client import VoyageClient

if TYPE_CHECKING:  # pragma: no cover — import-cycle dodge, type-only
    from lossless_hermes.engine import LCMEngine

__all__ = (
    "_adapt_lcm_describe",
    "_adapt_lcm_get_entity",
    "_adapt_lcm_grep",
    "_adapt_lcm_search_entities",
)


# ---------------------------------------------------------------------------
# Per-tool typed contexts — frozen dataclasses structurally satisfying the
# handlers' ``*Context`` Protocols.
# ---------------------------------------------------------------------------
#
# Each handler declares a narrow structural ``*Context`` Protocol (e.g.
# ``GrepContext``) exposing only the engine slice it needs. The engine
# class itself does NOT satisfy those Protocols (private ``Optional``
# attributes, different names). These frozen dataclasses are the
# translation: built fresh per dispatch with the engine's collaborators
# mapped onto the Protocols' public, non-optional shape. ``frozen=True``
# makes them immutable snapshots — a handler cannot mutate the engine's
# wiring through its ``ctx``.
#
# The dataclasses are local (not the handlers' own Protocols) so ``ty``
# structurally verifies the adapter ↔ Protocol match: passing a
# ``_GetEntityCtx`` where a ``GetEntityContext`` is expected only
# type-checks if the dataclass genuinely exposes every Protocol member
# with a compatible type.


@dataclass(frozen=True)
class _GetEntityCtx:
    """Frozen :class:`~lossless_hermes.tools.get_entity.GetEntityContext`.

    ``lcm_get_entity`` needs only a SQL connection and a timezone.
    """

    conn: sqlite3.Connection
    timezone: str


@dataclass(frozen=True)
class _SearchEntitiesCtx:
    """Frozen :class:`~..search_entities.SearchEntitiesContext`.

    ``lcm_search_entities`` needs a SQL connection, the conversation
    store (exposed for Protocol symmetry — the handler does not consult
    it), and a timezone.
    """

    conn: sqlite3.Connection
    conversation_store: ConversationStore
    timezone: str


@dataclass(frozen=True)
class _DescribeCtx:
    """Frozen :class:`~lossless_hermes.tools.describe.DescribeContext`.

    ``lcm_describe`` needs a SQL connection, the summary + conversation
    stores, a timezone, and the default expand-token budget cap.
    """

    conn: sqlite3.Connection
    summary_store: SummaryStore
    conversation_store: ConversationStore
    timezone: str
    max_expand_tokens: int


@dataclass(frozen=True)
class _GrepCtx:
    """Frozen :class:`~lossless_hermes.tools.grep.GrepContext`.

    ``lcm_grep`` needs a SQL connection, the summary + conversation
    stores, a timezone, an optional Voyage client (always ``None`` for
    v0.2.0 per ADR-033 — see :func:`_adapt_lcm_grep`), and the
    embeddings-opt-in flag.
    """

    conn: sqlite3.Connection
    summary_store: SummaryStore
    conversation_store: ConversationStore
    timezone: str
    voyage: Optional[VoyageClient]
    embeddings_enabled: bool


# ---------------------------------------------------------------------------
# Shared adapter helpers
# ---------------------------------------------------------------------------


def _engine_not_ready_error(tool: str, missing: str) -> str:
    """Build the structured tool-error for an unset engine collaborator.

    ``engine._db`` / the stores are ``None`` until ``on_session_start``.
    A tool dispatch always happens inside an active session, so these
    should be populated at call time — but if they are not, an adapter
    must degrade to a structured ``tool_result`` error rather than
    raising an :class:`AttributeError` (which PR-0's crash-hardening
    would catch, but an explicit, named error is clearer for the
    operator and the model).

    Args:
        tool: The tool name, for the error message.
        missing: The engine collaborator that was ``None``.

    Returns:
        A :func:`tool_result`-encoded ``{"error": ...}`` JSON string.
    """
    return tool_result({
        "error": (
            f"LCM tool {tool!r} cannot run: engine state is not "
            f"initialised ({missing} is unset). This tool must be "
            "called inside an active LCM session (after on_session_start)."
        )
    })


def _resolve_session_key(engine: LCMEngine, kwargs: dict[str, Any]) -> Optional[str]:
    """Resolve the session key for a tool dispatch.

    ``_dispatch_tool_call`` forwards the already-resolved ``session_key``
    in ``kwargs`` (``handle_tool_call`` resolves it from the kwarg chain,
    falling back to ``engine._current_session_key``). The adapter
    re-applies the ``engine._current_session_key`` fallback defensively
    in case a direct caller dispatched without it.

    Args:
        engine: The :class:`LCMEngine` from ``kwargs["ctx"]``.
        kwargs: The dispatch kwargs.

    Returns:
        The resolved session key, or ``None`` when no session is active.
    """
    return kwargs.get("session_key") or engine._current_session_key


def _build_deps(engine: LCMEngine) -> LcmDependencies:
    """Build the :class:`LcmDependencies` slice from engine state.

    :class:`LcmDependencies` is the narrow DI dataclass
    ``resolve_lcm_conversation_scope`` consumes. It carries a single
    field — ``resolve_session_id_from_session_key``, a
    ``Callable[[str], Optional[str]]`` consulted only in the scope
    resolver's step-4 fallback (when a ``session_id`` is absent but a
    ``session_key`` is present).

    In this Hermes port the engine's session key *is* its session id —
    ``_current_session_key`` is a property that returns
    ``current_session_id`` (the most-recent ``on_session_start``
    argument). So the resolver callback returns ``engine.current_session_id``:
    the genuine, non-inert session-key → session-id resolution for this
    engine. The adapters also pass ``session_id=<session_key>`` to the
    handlers directly, so the resolver's primary ``session_id`` path is
    already populated and this callback is a belt-and-braces fallback.

    Args:
        engine: The :class:`LCMEngine` from ``kwargs["ctx"]``.

    Returns:
        A :class:`LcmDependencies` with the resolver callback wired to
        the engine's current session id.
    """

    def _resolve_session_id(_session_key: str) -> Optional[str]:
        # The engine is single-session-scoped: session_key == session_id.
        return engine.current_session_id

    return LcmDependencies(resolve_session_id_from_session_key=_resolve_session_id)


# ---------------------------------------------------------------------------
# lcm_get_entity — Tier 1, no deps
# ---------------------------------------------------------------------------


def _adapt_lcm_get_entity(args: dict[str, Any], **kwargs: Any) -> str:
    """Dispatch adapter for ``lcm_get_entity`` (#156 PR-1).

    Builds a :class:`_GetEntityCtx` from the engine and calls
    :func:`~lossless_hermes.tools.get_entity.handle_lcm_get_entity`.
    ``lcm_get_entity`` takes no ``deps``.

    Args:
        args: The tool-call ``arguments`` dict (forwarded verbatim).
        **kwargs: The uniform dispatch kwargs — ``ctx`` (the engine),
            ``session_key``, ``runtime_ctx``, and any extras. Only
            ``ctx`` and ``session_key`` are consumed here.

    Returns:
        The handler's JSON string, or a structured ``tool_result``
        error if engine state is not initialised.
    """
    engine: LCMEngine = kwargs["ctx"]
    if engine._db is None:
        return _engine_not_ready_error("lcm_get_entity", "engine._db")

    ctx = _GetEntityCtx(conn=engine._db, timezone=engine.config.timezone)
    return handle_lcm_get_entity(
        args,
        ctx=ctx,
        session_key=_resolve_session_key(engine, kwargs),
    )


# ---------------------------------------------------------------------------
# lcm_search_entities — Tier 1, no deps
# ---------------------------------------------------------------------------


def _adapt_lcm_search_entities(args: dict[str, Any], **kwargs: Any) -> str:
    """Dispatch adapter for ``lcm_search_entities`` (#156 PR-1).

    Builds a :class:`_SearchEntitiesCtx` from the engine and calls
    :func:`~..search_entities.handle_lcm_search_entities`.
    ``lcm_search_entities`` takes no ``deps``.

    Args:
        args: The tool-call ``arguments`` dict (forwarded verbatim).
        **kwargs: The uniform dispatch kwargs — see
            :func:`_adapt_lcm_get_entity`.

    Returns:
        The handler's JSON string, or a structured ``tool_result``
        error if engine state is not initialised.
    """
    engine: LCMEngine = kwargs["ctx"]
    if engine._db is None:
        return _engine_not_ready_error("lcm_search_entities", "engine._db")
    if engine._conversation_store is None:
        return _engine_not_ready_error("lcm_search_entities", "engine._conversation_store")

    ctx = _SearchEntitiesCtx(
        conn=engine._db,
        conversation_store=engine._conversation_store,
        timezone=engine.config.timezone,
    )
    return handle_lcm_search_entities(
        args,
        ctx=ctx,
        session_key=_resolve_session_key(engine, kwargs),
    )


# ---------------------------------------------------------------------------
# lcm_describe — Tier 1, deps required
# ---------------------------------------------------------------------------


def _adapt_lcm_describe(args: dict[str, Any], **kwargs: Any) -> str:
    """Dispatch adapter for ``lcm_describe`` (#156 PR-1).

    Builds a :class:`_DescribeCtx` and a :class:`LcmDependencies` from
    the engine and calls
    :func:`~lossless_hermes.tools.describe.handle_lcm_describe`.

    ``is_subagent_session`` / ``grant_id_resolver`` are passed as
    ``None`` — the inert defaults. The handler's delegated-grant path is
    explicitly guarded on both being non-``None`` (``describe.py``
    ``_resolve_token_budget``), so ``None`` cleanly disables it. The
    delegated-expansion grant ledger is unported (ADR-012 sub-agent
    delegation is deferred), so there is no real resolver to wire.

    Args:
        args: The tool-call ``arguments`` dict (forwarded verbatim).
        **kwargs: The uniform dispatch kwargs — see
            :func:`_adapt_lcm_get_entity`.

    Returns:
        The handler's JSON string, or a structured ``tool_result``
        error if engine state is not initialised.
    """
    engine: LCMEngine = kwargs["ctx"]
    if engine._db is None:
        return _engine_not_ready_error("lcm_describe", "engine._db")
    if engine._summary_store is None:
        return _engine_not_ready_error("lcm_describe", "engine._summary_store")
    if engine._conversation_store is None:
        return _engine_not_ready_error("lcm_describe", "engine._conversation_store")

    ctx = _DescribeCtx(
        conn=engine._db,
        summary_store=engine._summary_store,
        conversation_store=engine._conversation_store,
        timezone=engine.config.timezone,
        max_expand_tokens=engine.config.max_expand_tokens,
    )
    session_key = _resolve_session_key(engine, kwargs)
    return handle_lcm_describe(
        args,
        ctx=ctx,
        deps=_build_deps(engine),
        session_key=session_key,
        # The engine is single-session-scoped: session_key == session_id.
        session_id=session_key,
        is_subagent_session=None,
        grant_id_resolver=None,
    )


# ---------------------------------------------------------------------------
# lcm_grep — Tier 2, deps required, Voyage off (ADR-033)
# ---------------------------------------------------------------------------


def _adapt_lcm_grep(args: dict[str, Any], **kwargs: Any) -> str:
    """Dispatch adapter for ``lcm_grep`` (#156 PR-1).

    Builds a :class:`_GrepCtx` and a :class:`LcmDependencies` from the
    engine and calls
    :func:`~lossless_hermes.tools.grep.handle_lcm_grep`.

    ``voyage`` is ``None`` for v0.2.0. Per ADR-033 embeddings are opt-in
    and off by default; ``lcm_grep``'s ``regex`` / ``full_text`` /
    ``verbatim`` modes never touch Voyage, and the ``hybrid`` /
    ``semantic`` modes refuse cleanly when ``voyage`` is ``None`` (and
    are gated even earlier by ``ctx.embeddings_enabled``, which is also
    ``False`` by default). Wiring a real :class:`VoyageClient` is a
    separate, post-v0.2.0 concern.

    Args:
        args: The tool-call ``arguments`` dict (forwarded verbatim).
        **kwargs: The uniform dispatch kwargs — see
            :func:`_adapt_lcm_get_entity`.

    Returns:
        The handler's JSON string, or a structured ``tool_result``
        error if engine state is not initialised.
    """
    engine: LCMEngine = kwargs["ctx"]
    if engine._db is None:
        return _engine_not_ready_error("lcm_grep", "engine._db")
    if engine._summary_store is None:
        return _engine_not_ready_error("lcm_grep", "engine._summary_store")
    if engine._conversation_store is None:
        return _engine_not_ready_error("lcm_grep", "engine._conversation_store")

    ctx = _GrepCtx(
        conn=engine._db,
        summary_store=engine._summary_store,
        conversation_store=engine._conversation_store,
        timezone=engine.config.timezone,
        # ADR-033: embeddings opt-in / off by default — no Voyage client
        # is wired for v0.2.0. hybrid / semantic modes refuse cleanly.
        voyage=None,
        embeddings_enabled=engine.config.embeddings_enabled,
    )
    session_key = _resolve_session_key(engine, kwargs)
    return handle_lcm_grep(
        args,
        ctx=ctx,
        deps=_build_deps(engine),
        session_key=session_key,
        # The engine is single-session-scoped: session_key == session_id.
        session_id=session_key,
    )
