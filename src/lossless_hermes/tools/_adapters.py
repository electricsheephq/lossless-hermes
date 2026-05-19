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
``lcm_search_entities``, ``lcm_describe``, ``lcm_grep``. #156 PR-2 adds
``lcm_compact``; ``lcm_synthesize_around`` ships in PR-3; ``lcm_expand``
is deferred per ADR-037.

The ``lcm_compact`` shim (#156 PR-2)
------------------------------------

``lcm_compact`` is the one ported tool whose ``*Context`` is NOT a
plain attribute bag. :class:`~lossless_hermes.tools.compact.CompactContext`
needs ``config: LcmConfig`` **plus two methods** —
``get_agent_compaction_gate_state(...)`` and ``compact(...)``. The
:class:`LCMEngine` satisfies neither directly:

* ``get_agent_compaction_gate_state`` does not exist on the engine at
  all. :class:`_CompactCtx` reimplements it from
  ``engine.info.owns_compaction`` + the per-call token snapshot — a
  faithful Python port of TS ``LcmContextEngine.getAgentCompactionGateState``
  (``lossless-claw/src/engine.ts:7118-7183`` @ ``1f07fbd``). It needs
  no engine state beyond ``info``, so the scoping plan's claim "the
  gate-state is synthesizable from ``engine.config`` + ``info.owns_compaction``
  + the ``RuntimeContext``" holds.
* ``compact`` *does* exist (:meth:`LCMEngine.compact`) but its signature
  diverges — the engine takes ``conversation_id: int`` where the
  Protocol passes ``session_id: str``. :class:`_CompactCtx.compact`
  bridges it exactly as the TS ``LcmContextEngine.compact`` envelope
  (``engine.ts:7185-7243``) does: resolve ``session_id`` →
  ``conversation_id`` via
  :meth:`ConversationStore.get_conversation_by_session_id`, returning
  the ``"no conversation found"`` no-op when none exists, and the
  ``"missing token budget in compact params"`` no-op when ``token_budget``
  is absent (the engine's ``compact()`` requires a non-``Optional``
  ``int`` budget — the TS ``executeCompactionCore`` guard at
  ``engine.ts:3363-3369`` is reproduced in the shim).

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

import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Optional

from lossless_hermes.compaction import CompactionResult
from lossless_hermes.db.config import LcmConfig
from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.store.summary import SummaryStore
from lossless_hermes.tools._common import tool_result
from lossless_hermes.tools.compact import (
    GateState,
    RuntimeContext,
    handle_lcm_compact,
)
from lossless_hermes.tools.conversation_scope import LcmDependencies
from lossless_hermes.tools.describe import handle_lcm_describe
from lossless_hermes.tools.get_entity import handle_lcm_get_entity
from lossless_hermes.tools.grep import handle_lcm_grep
from lossless_hermes.tools.search_entities import handle_lcm_search_entities
from lossless_hermes.voyage.client import VoyageClient

if TYPE_CHECKING:  # pragma: no cover — import-cycle dodge, type-only
    from lossless_hermes.engine import LCMEngine

logger = logging.getLogger("lossless_hermes.tools._adapters")

__all__ = (
    "_adapt_lcm_compact",
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
# lcm_compact — the 2-method context shim
# ---------------------------------------------------------------------------
#
# Unlike the four frozen attribute-bag contexts above,
# ``CompactContext`` (compact.py:387-437) requires ``config`` PLUS two
# methods. The engine satisfies neither method directly, so this shim
# *implements* both — it is a real object with behaviour, not a
# translation snapshot. ``ty`` structurally verifies the shim against
# ``CompactContext`` at the ``handle_lcm_compact(ctx=...)`` call site.
#
# ``frozen=True``: the shim is built once per dispatch and never mutated.
# ``config`` is the only stored field; the engine is captured as a
# private field so the two methods can reach ``engine.info`` /
# ``engine._conversation_store`` / ``engine.compact`` at call time.


# Default reserve-fraction floor for the gate-state check.
#
# ``reserve_fraction`` is NOT an ``LcmConfig`` field (confirmed: a grep
# of ``db/config.py`` finds it only as a ``CompactContext.compact``
# parameter, never a config attribute — the #156 scoping plan's 90%
# flag, now closed). The agent supplies it per-call via the
# ``reserveFraction`` tool arg; ``handle_lcm_compact`` parses + clamps
# it (``_resolve_reserve_fraction``, compact.py:568) and forwards the
# resolved value to ``get_agent_compaction_gate_state``. So the shim's
# gate-state method always *receives* a concrete float and never needs
# this default — it exists only to mirror the TS source's own default
# (``engine.ts:7148`` ``return 0.5``) and is applied defensively if a
# direct caller somehow passes a non-finite value.
_DEFAULT_RESERVE_FRACTION: Final[float] = 0.5

# Gate-state clamp bounds — TS ``engine.ts:7149`` ``Math.max(0.5,
# Math.min(1.0, r))``. Identical to the handler's own
# ``_RESERVE_FRACTION_FLOOR`` / ``_RESERVE_FRACTION_CEILING``
# (compact.py:192-198); duplicated here so the shim's gate-state method
# is a faithful standalone port of the TS engine method rather than
# relying on the handler having pre-clamped.
_RESERVE_FRACTION_FLOOR: Final[float] = 0.5
_RESERVE_FRACTION_CEILING: Final[float] = 1.0


@dataclass(frozen=True)
class _CompactCtx:
    """Shim implementing :class:`~lossless_hermes.tools.compact.CompactContext`.

    ``lcm_compact``'s context is the one ported ``*Context`` that is
    not a plain attribute bag — it needs ``config`` plus two methods.
    This shim is a real behavioural object: it stores the engine and
    implements both methods over engine state.

    Attributes:
        config: The validated :class:`LcmConfig` — ``CompactContext``
            requires it for the ``agent_compaction_tool_enabled`` flag
            (Stage 1 of the handler's gate sequence). Sourced verbatim
            from ``engine.config``.
        _engine: The :class:`LCMEngine` the two methods operate over.
            Private (leading underscore) so it is not mistaken for part
            of the ``CompactContext`` Protocol surface — the Protocol
            declares only ``config`` + the two methods.
    """

    config: LcmConfig
    _engine: LCMEngine

    def get_agent_compaction_gate_state(
        self,
        *,
        session_id: str,
        session_key: str,
        current_token_count: Optional[int],
        token_budget: Optional[int],
        reserve_fraction: float,
    ) -> GateState:
        """Reimplemented engine-side compaction gate.

        :class:`LCMEngine` has no ``get_agent_compaction_gate_state``
        method — this is a faithful Python port of TS
        ``LcmContextEngine.getAgentCompactionGateState``
        (``lossless-claw/src/engine.ts:7118-7183`` @ ``1f07fbd``). It
        needs no engine state beyond ``engine.info`` (the capability
        record carrying ``owns_compaction``), so it is fully
        synthesizable per the #156 scoping plan.

        Gates checked, first refusal wins (TS order, ``engine.ts:7101``):

        1. **``owns_compaction``** — when the engine does not own
           compaction (migration failed at boot — ``engine.info``
           degraded to ``owns_compaction=False``), refuse with
           ``engine-unhealthy``. TS ``engine.ts:7134-7144``.
        2. **below-floor** — when ``current_token_count / token_budget``
           is below ``reserve_fraction``, refuse with ``below-floor``.
           Only meaningful when both token figures are present and
           valid; absent telemetry skips the check (the gate accepts).
           TS ``engine.ts:7152-7175``.

        Deliberately NOT gated (TS ``engine.ts:7105-7116``): prompt-cache
        hot/cold state — agent-triggered compaction is a conscious
        trade. Auth circuit-breaker / session-exclusion surface inside
        :meth:`compact` itself, not here.

        Args:
            session_id: The runtime session id. Unused by the gate
                logic (the TS source likewise ignores it) — accepted
                for Protocol conformance.
            session_key: The session-family key. Unused — as above.
            current_token_count: Live observed token count, or ``None``
                when no LLM call has fired this session.
            token_budget: Effective context budget, or ``None`` when
                not yet inferred.
            reserve_fraction: Lower bound on the context ratio before
                compaction is allowed. ``handle_lcm_compact`` has
                already parsed + clamped this from the ``reserveFraction``
                tool arg; the shim re-clamps defensively so it is a
                standalone faithful port of the TS method.

        Returns:
            A :class:`GateState` — ``should_refuse=True`` with a
            populated ``refusal_reason`` / ``refusal_note`` on a
            refusal, ``should_refuse=False`` otherwise. ``context_ratio``
            is echoed back for diagnostics when computable.
        """
        # Gate 1 — owns_compaction (TS engine.ts:7134-7144).
        if self._engine.info.owns_compaction is not True:
            return GateState(
                owns_compaction=False,
                below_floor=False,
                should_refuse=True,
                refusal_reason="engine-unhealthy",
                refusal_note=(
                    "LCM engine migration did not complete at boot — compaction "
                    "unavailable until the gateway restarts cleanly."
                ),
            )

        # Clamp reserve_fraction to [0.5, 1.0]; non-finite → default.
        # TS engine.ts:7146-7150.
        if not isinstance(reserve_fraction, (int, float)) or isinstance(reserve_fraction, bool):
            clamped_reserve = _DEFAULT_RESERVE_FRACTION
        elif reserve_fraction != reserve_fraction or reserve_fraction in (
            float("inf"),
            float("-inf"),
        ):
            clamped_reserve = _DEFAULT_RESERVE_FRACTION
        else:
            clamped_reserve = min(
                _RESERVE_FRACTION_CEILING,
                max(_RESERVE_FRACTION_FLOOR, float(reserve_fraction)),
            )

        # Gate 2 — below-floor (TS engine.ts:7152-7175). The ratio is
        # only meaningful when both token figures are present + valid.
        have_budget = (
            isinstance(token_budget, int)
            and not isinstance(token_budget, bool)
            and token_budget > 0
        )
        have_current = (
            isinstance(current_token_count, int)
            and not isinstance(current_token_count, bool)
            and current_token_count >= 0
        )
        context_ratio: Optional[float] = None
        if have_budget and have_current:
            # Guarded by have_budget / have_current — both are real ints.
            context_ratio = current_token_count / token_budget  # type: ignore[operator]

        if context_ratio is not None and context_ratio < clamped_reserve:
            return GateState(
                owns_compaction=True,
                below_floor=True,
                should_refuse=True,
                refusal_reason="below-floor",
                refusal_note=(
                    f"Context is at {context_ratio * 100:.1f}% of budget — below "
                    f"the {clamped_reserve * 100:.0f}% floor. No need to compact "
                    "yet; chained tool calls have headroom."
                ),
                context_ratio=context_ratio,
            )

        return GateState(
            owns_compaction=True,
            below_floor=False,
            should_refuse=False,
            context_ratio=context_ratio,
        )

    def compact(
        self,
        *,
        session_id: str,
        session_key: str,
        session_file: str,
        token_budget: Optional[int],
        current_token_count: Optional[int],
        force: bool,
    ) -> CompactionResult:
        """Bridge the Protocol's ``compact`` to :meth:`LCMEngine.compact`.

        :meth:`LCMEngine.compact` (``engine/compact.py:940``) exists but
        its signature diverges from this Protocol method: the engine
        takes ``conversation_id: int`` + non-``Optional`` ``token_budget:
        int`` / ``current_tokens: int``, while the Protocol passes
        ``session_id: str`` + ``Optional[int]`` token figures. This
        bridge mirrors the TS ``LcmContextEngine.compact`` envelope
        (``engine.ts:7185-7243``):

        1. **session → conversation** — resolve ``session_id`` to a
           ``conversation_id`` via
           :meth:`ConversationStore.get_conversation_by_session_id`
           (TS ``engine.ts:7218-7228`` ``getConversationForSession``).
           When no conversation exists, return the
           ``"no conversation found"`` no-op — the handler's
           :func:`~lossless_hermes.tools.compact._map_engine_reason`
           maps it to the ``no-conversation`` tool reason.
        2. **missing budget** — the engine's ``compact()`` requires a
           concrete ``int`` budget; it cannot represent "budget absent".
           The TS ``executeCompactionCore`` guard (``engine.ts:3363-3369``)
           returns ``{ok: false, compacted: false, reason: "missing token
           budget in compact params"}`` in that case, so the shim
           reproduces that guard *before* delegating. The no-op carries
           ``reason="missing token budget in compact params"`` and
           ``auth_failure=False``; the handler's
           :func:`~lossless_hermes.tools.compact._result_ok` recognises
           the reason as a non-auth failure and reports ``ok=false``
           (matching TS), and :func:`~..compact._map_engine_reason`
           maps it to the ``missing-budget`` tool reason. It is NOT
           mislabelled as an auth failure — it is honestly a budget
           problem.
        3. **delegate** — call :meth:`LCMEngine.compact` with the
           resolved ``conversation_id``, the concrete budget, and the
           observed token count (defaulting ``current_token_count`` to
           ``0`` — the engine uses it only for breaker-open telemetry).

        ``session_key`` / ``session_file`` / ``force`` are accepted for
        Protocol conformance. ``force`` is forwarded to the engine via
        no path because :meth:`LCMEngine.compact` has no ``force``
        parameter — and the handler always passes ``force=False`` so the
        engine-side cache / threshold gates stay authoritative (the
        handler's own docstring, Stage 6). Were the handler ever to
        pass ``force=True``, this shim has no way to honour it; that is
        a documented, currently-unreachable limitation. As a defence
        against a future handler change that *does* start passing
        ``force=True``, the shim emits a ``logger.warning`` in that case
        so the dropped flag is visible in the gateway log rather than a
        silent no-op.

        Args:
            session_id: The runtime session id — resolved to a
                conversation here.
            session_key: The session-family key. Unused — the engine's
                ``compact()`` is conversation-scoped and this engine is
                single-session (``session_key == session_id``).
            session_file: Passthrough session-file path. Unused — the
                engine resolves its own conversation; the field is
                deprecated on :class:`~..compact.RuntimeContext`.
            token_budget: Effective context budget, or ``None``.
            current_token_count: Live observed token count, or ``None``.
            force: Whether to force compaction. The handler always
                passes ``False``; see above.

        Returns:
            A :class:`CompactionResult` — either the engine's verbatim
            result, or a synthesized no-op for the
            no-conversation / missing-budget short-circuits.
        """
        # Defensive: handle_lcm_compact hardcodes force=False (Stage 6),
        # and LCMEngine.compact() has no force parameter, so force=True is
        # currently unreachable and would be silently dropped. Warn so a
        # future handler change that starts passing force=True is caught
        # in the gateway log rather than producing a silent no-op.
        if force:
            logger.warning(
                "[lcm] _CompactCtx.compact received force=True, but "
                "LCMEngine.compact() has no force parameter — the flag is "
                "dropped. Engine-side cache / threshold gates stay "
                "authoritative. (handle_lcm_compact currently always "
                "passes force=False; this path indicates a handler change.)"
            )
        del session_key, session_file, force  # see docstring

        store = self._engine._conversation_store
        if store is None:
            # on_session_start has not run — no DB. The adapter's
            # engine-readiness guard catches this before the shim is
            # ever built, but the method must be self-consistent.
            return CompactionResult(
                action_taken=False,
                tokens_before=current_token_count or 0,
                tokens_after=current_token_count or 0,
                created_summary_id=None,
                condensed=False,
                level=None,
                passes_completed=0,
                auth_failure=False,
                reason="no conversation found for session",
            )

        # Step 1: session → conversation (TS engine.ts:7218-7228).
        conversation = store.get_conversation_by_session_id(session_id)
        if conversation is None:
            return CompactionResult(
                action_taken=False,
                tokens_before=current_token_count or 0,
                tokens_after=current_token_count or 0,
                created_summary_id=None,
                condensed=False,
                level=None,
                passes_completed=0,
                auth_failure=False,
                reason="no conversation found for session",
            )

        # Step 2: missing-budget guard (TS engine.ts:3363-3369). The
        # engine's compact() requires a concrete int budget.
        have_budget = (
            isinstance(token_budget, int)
            and not isinstance(token_budget, bool)
            and token_budget > 0
        )
        if not have_budget:
            return CompactionResult(
                action_taken=False,
                tokens_before=current_token_count or 0,
                tokens_after=current_token_count or 0,
                created_summary_id=None,
                condensed=False,
                level=None,
                passes_completed=0,
                auth_failure=False,
                reason="missing token budget in compact params",
            )

        # Step 3: delegate to the real engine.compact(). Past the
        # have_budget guard ``token_budget`` is a real positive int (the
        # engine's compact() takes a non-Optional ``int`` budget).
        # current_tokens defaults to 0 (the engine uses it only for
        # breaker-open telemetry).
        return self._engine.compact(
            conversation_id=conversation.conversation_id,
            token_budget=token_budget,
            current_tokens=current_token_count or 0,
        )


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


# ---------------------------------------------------------------------------
# lcm_compact — Tier 3, 2-method context shim (#156 PR-2)
# ---------------------------------------------------------------------------


def _adapt_lcm_compact(args: dict[str, Any], **kwargs: Any) -> str:
    """Dispatch adapter for ``lcm_compact`` (#156 PR-2).

    The hardest of the #156 adapters: ``lcm_compact``'s context is the
    one ported ``*Context`` that is not a plain attribute bag. It builds
    a :class:`_CompactCtx` — a behavioural shim implementing
    :class:`~lossless_hermes.tools.compact.CompactContext`'s ``config``
    field plus its ``get_agent_compaction_gate_state`` /  ``compact``
    methods — and calls
    :func:`~lossless_hermes.tools.compact.handle_lcm_compact`.

    Two adapter-specific concerns beyond the PR-1 pattern:

    * **``runtime_ctx`` → ``runtime_context``** — ``_dispatch_tool_call``
      forwards the token snapshot under the kwarg name ``runtime_ctx``
      (an :class:`lossless_hermes.engine.RuntimeContext`), but
      ``handle_lcm_compact`` declares the parameter ``runtime_context``
      (a :class:`lossless_hermes.tools.compact.RuntimeContext` — a
      *different* class, with an extra ``session_file`` field). This
      adapter translates the engine snapshot into the handler's
      ``RuntimeContext`` and passes it under the correct name. The
      handler tolerates ``None`` (treats it as an empty snapshot), so a
      missing / unrecognised ``runtime_ctx`` degrades cleanly.

    * **``ctx is None``** — unlike the four PR-1 tools, ``lcm_compact``
      treats a ``None`` ``ctx`` as a first-class state (the handler
      returns ``engine-unavailable``). The adapter still builds a real
      :class:`_CompactCtx` here whenever the engine is present — engine
      readiness is checked the same way as the other adapters, but the
      *unset-engine-state* failure is surfaced as the handler's own
      ``engine-unavailable`` reason rather than the adapters'
      ``_engine_not_ready_error`` string, because that is the
      tool-facing contract ``lcm_compact`` already defines.

    Args:
        args: The tool-call ``arguments`` dict (forwarded verbatim —
            ``handle_lcm_compact`` reads ``reserveFraction`` from it).
        **kwargs: The uniform dispatch kwargs — ``ctx`` (the engine),
            ``session_key``, ``runtime_ctx``, and any extras.

    Returns:
        The handler's JSON string. ``handle_lcm_compact`` is itself
        exception-tolerant (its Stage-6 ``try/except`` converts an
        engine throw into a structured ``exception`` reason), and PR-0's
        crash-hardening in ``_dispatch_tool_call`` is the outer backstop.
    """
    engine: LCMEngine = kwargs["ctx"]

    # lcm_compact's handler defines ``engine-unavailable`` as the
    # tool-facing reason for "engine state not ready". Surface that
    # contract by passing ctx=None to the handler when the engine's
    # conversation store has not been brought up (on_session_start has
    # not run) — rather than the adapters' generic _engine_not_ready_error.
    # The handler's Stage-1/2 gates then emit the proper structured reason.
    if engine._conversation_store is None:
        return handle_lcm_compact(args, ctx=None)

    ctx = _CompactCtx(config=engine.config, _engine=engine)
    session_key = _resolve_session_key(engine, kwargs)

    # Translate the engine's RuntimeContext (kwarg ``runtime_ctx``) into
    # the handler's RuntimeContext (kwarg ``runtime_context``). The two
    # are distinct classes; the handler's carries an extra ``session_file``
    # (left ``None`` — the shim's compact() resolves its own conversation
    # and ignores session_file). A missing / wrong-typed runtime_ctx
    # degrades to None, which the handler treats as an empty snapshot.
    engine_rt = kwargs.get("runtime_ctx")
    runtime_context: Optional[RuntimeContext] = None
    if engine_rt is not None:
        runtime_context = RuntimeContext(
            current_token_count=getattr(engine_rt, "current_token_count", None),
            token_budget=getattr(engine_rt, "token_budget", None),
        )

    return handle_lcm_compact(
        args,
        ctx=ctx,
        session_key=session_key,
        # The engine is single-session-scoped: session_key == session_id.
        session_id=session_key,
        # Kwarg rename: _dispatch_tool_call forwards ``runtime_ctx``;
        # handle_lcm_compact's parameter is ``runtime_context``.
        runtime_context=runtime_context,
    )
