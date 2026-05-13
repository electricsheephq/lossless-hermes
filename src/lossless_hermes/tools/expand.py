"""Port of ``lcm_expand`` — sub-agent-only DAG expansion primitive.

Ports ``lossless-claw/src/tools/lcm-expand-tool.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 455 LOC TS → ~420 LOC Python). The
TypeBox-declared schema lives at TS lines 24-66; the handler body at
lines 144-453. Both are translated structurally verbatim per ADR-016
(description prose byte-identical from TS source).

What this tool does
-------------------

``lcm_expand`` is the **PRIMITIVE expand tool** — invoked ONLY from a
delegated sub-agent session. Main-agent sessions get a structured error
and a documented fallback (use ``lcm_describe`` with
``expandChildren`` / ``expandMessages`` flags for one-hop drilldown, or
``lcm_expand_query`` for the multi-hop delegated path).

When invoked from a sub-agent session, the tool expands the LCM summary
DAG to retrieve children and source messages under a budget cap. Two
entry shapes are supported:

1. **summaryIds** — direct expansion of a caller-supplied list of
   ``sum_xxx`` IDs. The orchestrator walks each one BFS under the token
   cap and emits the merged result.
2. **query** — grep-first then expand: the handler runs
   ``retrieval.grep({query, mode: "full_text"})`` to find candidate
   summary IDs, then drives the orchestrator on the top matches.

Output is a compact text payload (``distill_for_subagent``) plus a
``citedIds`` list that the sub-agent can quote back to its parent.

ADR-012 (subagent defer) — what's in vs out
-------------------------------------------

Per [ADR-012](../../docs/adr/012-subagent-defer.md), the WRAPPER tool
``lcm_expand_query`` (the convenience tool that auto-dispatches a
sub-agent) is **deferred to v2**. The PRIMITIVE ``lcm_expand`` (this
module) ships in v0.1.0. The schema description text still references
``lcm_expand_query`` even though that tool isn't registered in v0.1.0
— this is intentional per the issue spec (the model won't try to call
an unregistered tool, and the prose is byte-identical to the TS source
per ADR-016).

What ISN'T ported here (compared to the TS source):

* The ``runDelegatedExpansionLoop`` policy branch (TS lines 264-302,
  381-411) — only ``lcm_expand_query`` would use it. The Python port
  collapses the dispatch to the direct-orchestrator path only.
* The ``decideLcmExpansionRouting`` policy stub — same reason. The
  ``policy`` / ``executionPath`` / ``delegated`` /
  ``observability`` keys in ``details`` are omitted from the v0.1.0
  payload.
* ``lcm-expand-tool.delegation.ts`` (580 LOC) — only used by
  ``lcm_expand_query``. Not in this issue's scope.

Wave-N invariants (preserved per ADR-029)
-----------------------------------------

* **NOT wrapped in ``run_with_token_gate``.** Per ``tools.md`` line 638
  and the needs-compact-gate.ts docstring (line 38), ``lcm_expand`` is
  in ``TOKEN_GATE_TOOLS`` bypass set. The grant ledger does its own
  (sub-agent-scoped) budget gating, so the global compaction gate
  would double-charge.
* **Wave-12 F5 (middleware-not-decorator)** — even though this tool is
  bypassed, the dispatch layer still wraps it as middleware (not at
  registration time). The middleware checks the bypass set; the
  decision happens at invocation time. The bypass keeps the wave-12
  invariant intact (per ADR-029 Wave-12 F5 row).
* **No Wave-N inline comments in this file.** The TS source has none
  in the ``lcm-expand-tool.ts`` proper; the Wave-N scar-tissue sits
  in the downstream ``expansion.ts`` / ``expansion-auth.ts`` modules
  (separate ports).

Architecture seams
------------------

This handler defers to two injected protocols:

* :class:`ExpansionOrchestrator` — exposes ``expand({summaryIds,
  conversationId, maxDepth, tokenCap, includeMessages}) ->
  ExpansionResult`` for the BFS walk. Production callers wire this
  via :mod:`lossless_hermes.expansion` (when it lands — see
  ``epics/03-engine`` for the port). Tests construct a stub that
  returns deterministic ``ExpansionResult`` instances.

* :class:`Retrieval` — exposes ``grep({query, mode, scope,
  conversationId}) -> GrepResult`` for the query-entry path.
  Production callers wire this through the same retrieval engine
  (Epic 01) used by ``lcm_grep``. Tests construct a stub.

Plus three callable injection seams shared with describe.py
(see :func:`handle_lcm_expand` docstring):

* ``is_subagent_session(session_key) -> bool`` — the predicate this
  tool gates on. Production: checks for the ``:subagent:`` substring
  in the session key (matches the TS test fixture pattern at
  ``test/lcm-expand-tool.test.ts:71``: ``sessionKey.includes(":subagent:")``).
  Tests can override.
* ``grant_id_resolver(session_key) -> grant_id | None`` — resolves a
  delegated-expansion grant id for a sub-agent session. Production:
  wired through ``expansion_auth`` (not yet ported; per ADR-012 the
  full grant ledger ports in v2). Tests construct a stub returning
  either a valid grant_id string or None.
* ``runtime_auth_manager`` — the in-memory grant manager that
  exposes ``get_grant(grant_id)`` and the ``wrap_with_auth`` factory.
  Not ported in v0.1.0 either; the handler short-circuits the
  wrap-with-auth path when the manager is None.

Source map
----------

* TS canonical: ``lossless-claw/src/tools/lcm-expand-tool.ts:1-455``.
* Porting guide: ``docs/porting-guides/tools.md`` §"lcm_expand"
  (lines 202-256).
* Issue spec: ``epics/06-tools/06-12-lcm-expand.md``.
* [ADR-012](../../docs/adr/012-subagent-defer.md) — why
  ``lcm_expand_query`` is NOT ported alongside.
* [ADR-016](../../docs/adr/016-typebox-translation.md) — TypeBox
  hand-translate policy (description prose byte-identical).
* [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-N
  provenance (none in this file; downstream modules).
* TS test fixture: ``test/lcm-expand-tool.test.ts`` (496 LOC).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Optional, Protocol

from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.tools import TOOL_SCHEMAS
from lossless_hermes.tools._common import (
    read_bool_param,
    read_string_param,
    tool_result,
)
from lossless_hermes.tools._typebox import (
    array_field,
    boolean_field,
    number_field,
    object_schema,
    optional,
    string_field,
    tool_schema,
)
from lossless_hermes.tools.conversation_scope import (
    LcmDependencies,
    resolve_lcm_conversation_scope,
)

__all__ = (
    "LCM_EXPAND_DESCRIPTION",
    "LCM_EXPAND_SCHEMA",
    "ExpandContext",
    "ExpansionOrchestrator",
    "ExpansionResult",
    "GrepResult",
    "GrepSummaryMatch",
    "Retrieval",
    "default_is_subagent_session_key",
    "handle_lcm_expand",
)


# ===========================================================================
# Schema — verbatim from TS source (ADR-016 §Consequences)
# ===========================================================================
#
# Description prose is byte-identical to lcm-expand-tool.ts lines
# 134-142 (the `description:` block) and the per-field `description`
# strings at lines 24-66. The mechanical TypeBox → dict translation
# uses the helpers in `_typebox.py`.

LCM_EXPAND_DESCRIPTION: Final[str] = (
    "SUB-AGENT ONLY. Main-agent sessions get a runtime error if they invoke "
    "this tool — instead, main agents should use lcm_describe with "
    "expandChildren/expandMessages flags (one-hop drilldown), or "
    "lcm_expand_query (delegated multi-hop drilldown that spawns a sub-agent). "
    "When called from a sub-agent: expands the LCM summary DAG to retrieve "
    "children and source messages. Provide summaryIds (direct expansion) or "
    "query (grep-first, then expand top matches). Returns a compact text "
    "payload plus cited IDs for follow-up."
)
"""Verbatim from ``lcm-expand-tool.ts:134-142``. Per ADR-016 §Consequences
this is the load-bearing model-facing prose that drives tool selection.
The ``lcm_expand_query`` reference is intentional even though that tool
isn't registered in v0.1.0 — per ADR-012 it's deferred, but the prose
stays verbatim. The model won't try to call an unregistered tool."""


LCM_EXPAND_SCHEMA: Final[dict[str, Any]] = tool_schema(
    name="lcm_expand",
    description=LCM_EXPAND_DESCRIPTION,
    parameters=object_schema(
        summaryIds=optional(
            array_field(
                string_field(),
                description=(
                    "Summary IDs to expand (sum_xxx format). Required if query is not provided."
                ),
            ),
        ),
        query=optional(
            string_field(
                "Text query to grep for matching summaries before expanding. "
                "If provided, summaryIds is ignored and the top grep results are expanded.",
            ),
        ),
        maxDepth=optional(
            number_field(
                "Max traversal depth per summary (default: 3).",
                minimum=1,
            ),
        ),
        tokenCap=optional(
            number_field(
                "Max tokens across the entire expansion result.",
                minimum=1,
            ),
        ),
        includeMessages=optional(
            boolean_field(
                "Whether to include raw source messages at leaf level (default: false).",
            ),
        ),
        conversationId=optional(
            number_field(
                "Conversation ID to scope the expansion to. If omitted, uses the current session's conversation.",
            ),
        ),
        allConversations=optional(
            boolean_field(
                "Set true to explicitly allow cross-conversation expansion. Ignored when conversationId is provided.",
            ),
        ),
    ),
)
"""OpenAI-function-call schema for ``lcm_expand``. Verbatim translation
of the TypeBox declaration at ``lcm-expand-tool.ts:24-66`` per ADR-016.

The schema's ``required`` array is empty: at least one of
``summaryIds`` / ``query`` must be present, validated at runtime per
the issue spec AC. The TypeBox source uses ``Type.Optional(...)`` for
every field — the schema-validator doesn't enforce the
"at-least-one-of" constraint; the handler does."""


# Register at module import time per the TOOL_SCHEMAS contract documented
# in tools/__init__.py. The 06-02 dispatch table reads via
# ``get_tool_schemas()`` so this side-effect is what makes the tool
# discoverable to the LCMEngine.
TOOL_SCHEMAS.append(LCM_EXPAND_SCHEMA)


# ===========================================================================
# Defaults & constants — match TS source
# ===========================================================================

_DEFAULT_MAX_DEPTH: Final[int] = 3
"""Default ``maxDepth`` when caller omits it (TS line 117)."""

_SUBAGENT_SUBSTRING: Final[str] = ":subagent:"
"""Substring that flags a session key as a delegated sub-agent session.

Mirrors the TS test fixture's predicate at
``test/lcm-expand-tool.test.ts:71``::

    isSubagentSessionKey: (sessionKey: string) => sessionKey.includes(":subagent:")

Per the issue's "Open question": Hermes's session-key model is
``agent:profile:session``, and delegated children get a ``:subagent:``
suffix injected by the gateway. The :func:`default_is_subagent_session_key`
predicate below applies this convention; callers wiring the real
dispatch can override via the ``is_subagent_session`` injection seam
on :func:`handle_lcm_expand`."""


# ===========================================================================
# Error prose — verbatim from TS source
# ===========================================================================
#
# Pinned by tests against TS line 168 + line 183. Keep byte-identical
# so a Wave-13+ TS bump that tweaks the wording surfaces as a test
# delta (per ADR-016 description-string verbatim rule).

_MAIN_AGENT_REFUSAL_ERROR: Final[str] = (
    "lcm_expand is only available in sub-agent sessions. Use "
    "lcm_expand_query to ask a focused question against expanded "
    "summaries, or lcm_describe/lcm_grep for lighter lookups."
)
"""Verbatim from ``lcm-expand-tool.ts:166-169``. Pinned by test."""

_NO_GRANT_ERROR: Final[str] = (
    "Delegated expansion requires a valid grant. This sub-agent "
    "session has no propagated expansion grant."
)
"""Verbatim from ``lcm-expand-tool.ts:181-184``. Pinned by test."""

_NO_CONVERSATION_ERROR: Final[str] = (
    "No LCM conversation found for this session. Provide conversationId or "
    "set allConversations=true."
)
"""Mirrors the conversation-scope error prose used by ``describe.py``.
Pinned by test."""

_NEITHER_SUMMARY_IDS_NOR_QUERY_ERROR: Final[str] = "Either summaryIds or query must be provided."
"""Verbatim from ``lcm-expand-tool.ts:450-452``. Pinned by test."""


# ===========================================================================
# Public dataclasses + Protocols
# ===========================================================================


@dataclass(frozen=True)
class ExpansionResult:
    """The expand-orchestrator output.

    Mirrors the TS ``ExpansionResult`` type at
    ``lossless-claw/src/expansion.ts:20-43``. The Python port stores
    ``expansions`` as a list of dicts (rather than typed nested
    dataclasses) to keep the JSON round-trip trivial — the tool handler
    just serializes them straight to the wire payload. The full typed
    shape (with ``children`` and ``messages`` sublists) lives in the
    upcoming ``expansion.py`` port; this dataclass is a stable wire
    contract for the test seams.

    Attributes:
        expansions: List of per-summary expansion entries. Each entry is
            a dict with keys ``summaryId``, ``children``, ``messages``.
            The expand orchestrator is the source of truth for the
            exact shape; this dataclass is a structural pass-through.
        cited_ids: Distinct summary IDs that the orchestrator visited
            during the walk. The sub-agent uses these in citations.
        total_tokens: Cumulative tokens emitted across all entries.
        truncated: True when at least one entry was cut short by the
            token cap.
    """

    expansions: list[dict[str, Any]] = field(default_factory=list)
    cited_ids: list[str] = field(default_factory=list)
    total_tokens: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class GrepSummaryMatch:
    """One summary-match row from the grep path.

    Mirrors the TS ``GrepResult["summaries"][number]`` shape (from
    ``retrieval.ts``). Only the ``summary_id`` field is consumed by the
    expand handler; the rest is forwarded for downstream observability
    if the caller wants it.
    """

    summary_id: str


@dataclass(frozen=True)
class GrepResult:
    """The grep-engine output consumed by the query-entry path.

    Mirrors the TS ``GrepResult`` shape (from ``retrieval.ts``). Only
    the ``summaries`` list is consumed here; ``messages`` and
    ``total_matches`` are passed-through-able.
    """

    summaries: list[GrepSummaryMatch] = field(default_factory=list)


class Retrieval(Protocol):
    """Narrow Protocol for the grep-first path.

    Production callers wire this via :class:`RetrievalEngine` (Epic 01,
    not yet ported). Tests construct a stub that returns deterministic
    :class:`GrepResult` instances.

    Required methods:

    * ``grep(query, mode, scope, conversation_id) -> GrepResult`` —
      run a grep against the conversation-scoped corpus. ``mode`` is
      ``"full_text"`` for the lcm_expand query path (TS line 218).
      ``scope`` is ``"summaries"`` (TS line 252). ``conversation_id``
      is the resolved scope from
      :func:`resolve_lcm_conversation_scope`.
    """

    def grep(
        self,
        *,
        query: str,
        mode: str,
        scope: str,
        conversation_id: Optional[int],
    ) -> GrepResult: ...


class ExpansionOrchestrator(Protocol):
    """Narrow Protocol for the DAG-walking orchestrator.

    Production callers wire this via the :class:`ExpansionOrchestrator`
    class in :mod:`lossless_hermes.expansion` (not yet ported — issue
    in epic 03). Tests construct a stub that returns deterministic
    :class:`ExpansionResult` instances.

    Required method:

    * ``expand(summary_ids, conversation_id, max_depth, token_cap,
      include_messages) -> ExpansionResult`` — walk the DAG breadth-
      first under the token cap. With ``include_messages=True``,
      hydrates leaf messages.
    """

    def expand(
        self,
        *,
        summary_ids: list[str],
        conversation_id: int,
        max_depth: Optional[int] = None,
        token_cap: Optional[int] = None,
        include_messages: bool = False,
    ) -> ExpansionResult: ...


class ExpandContext(Protocol):
    """The handler's collaborator surface.

    Mirrors the slice of :class:`~lossless_hermes.engine.LCMEngine` that
    ``lcm_expand`` actually needs. Using a structural Protocol keeps
    the handler decoupled from the engine class shape and lets tests
    construct a tiny stand-in dataclass.

    Required attributes:

    * ``conn``: :class:`sqlite3.Connection` — present for symmetry with
      ``lcm_describe`` and ``lcm_search_entities``; the handler itself
      doesn't run direct SQL (the orchestrator and grep stubs do).
    * ``conversation_store``: :class:`ConversationStore` — for the
      conversation scope resolver via the ``_LcmLike`` Protocol shape.
    * ``orchestrator``: :class:`ExpansionOrchestrator` — the DAG walker.
    * ``retrieval``: :class:`Retrieval` — the grep engine for the
      query-entry path.
    """

    conn: sqlite3.Connection
    conversation_store: ConversationStore
    orchestrator: ExpansionOrchestrator
    retrieval: Retrieval


# ===========================================================================
# default_is_subagent_session_key — substring-match predicate
# ===========================================================================


def default_is_subagent_session_key(session_key: str) -> bool:
    """Return True when ``session_key`` is a delegated sub-agent session.

    Per the issue spec's "Open question": Hermes's session-key model is
    ``agent:profile:session``, and delegated children get a
    ``:subagent:`` suffix injected by the gateway (or by the
    ``delegate_task`` tool dispatch).

    Mirrors the TS test fixture's predicate at
    ``test/lcm-expand-tool.test.ts:71``::

        isSubagentSessionKey: (sessionKey: string) =>
            sessionKey.includes(":subagent:")

    The substring (not prefix) match handles the multiple-layer naming
    convention in Hermes — e.g. ``agent:main:subagent:foo`` and
    ``agent:lcm:subagent:bar`` are both delegated.

    Args:
        session_key: The session key to inspect. ``None`` / empty
            string returns ``False`` (no subagent marker can be present).

    Returns:
        ``True`` when ``session_key`` contains ``":subagent:"``;
        ``False`` otherwise (including for ``None`` / empty / non-string
        inputs — guards against caller misuse).
    """
    if not isinstance(session_key, str):
        return False
    return _SUBAGENT_SUBSTRING in session_key


# ===========================================================================
# _LcmScopeAdapter — bridge ExpandContext to the conversation_scope Protocol
# ===========================================================================


@dataclass
class _LcmScopeAdapter:
    """Adapter satisfying :class:`~..conversation_scope._LcmLike`.

    The conversation-scope resolver consumes a ``_LcmLike`` protocol —
    anything with a ``_conversation_store`` attribute. We don't expose
    a private attribute on :class:`ExpandContext`, so adapt at the
    call site. Same pattern as ``describe.py:_LcmScopeAdapter``.
    """

    _conversation_store: Optional[ConversationStore]


# ===========================================================================
# Handler entry point
# ===========================================================================


def handle_lcm_expand(  # noqa: PLR0912, PLR0915 — mirrors TS branch structure
    args: dict[str, Any],
    *,
    ctx: ExpandContext,
    deps: LcmDependencies,
    session_key: Optional[str] = None,
    session_id: Optional[str] = None,
    is_subagent_session: Optional[Callable[[str], bool]] = None,
    grant_id_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Handle an ``lcm_expand`` tool call.

    **Sub-agent-only:** main-agent sessions are refused with the
    structured error pinned by :data:`_MAIN_AGENT_REFUSAL_ERROR`.

    **Token-gate bypass:** per ``tools.md`` line 638 and
    ``needs-compact-gate.ts:38``, ``lcm_expand`` is in the
    ``TOKEN_GATE_TOOLS`` bypass set. The dispatch layer at issue 06-02
    (``LCMEngine.handle_tool_call``) skips the ``run_with_token_gate``
    wrap for this tool name. The grant ledger does its own
    sub-agent-scoped budget enforcement (TS lines 175-178; not yet
    ported — ADR-012 defers the auth manager).

    Args:
        args: The tool-call ``arguments`` dict from the LLM provider.
            Read defensively — see :mod:`lossless_hermes.tools._common`.
        ctx: An :class:`ExpandContext` exposing the orchestrator +
            retrieval + conversation-store collaborator surface.
        deps: :class:`LcmDependencies` slice (the same dataclass
            ``resolve_lcm_conversation_scope`` consumes).
        session_key: Optional cross-conversation session-family key.
            The sub-agent gate is keyed off this (or ``session_id`` as
            a fallback, mirroring TS lines 163-164).
        session_id: Optional runtime session id. Either ``session_key``
            or ``session_id`` should be supplied so the gate can
            evaluate.
        is_subagent_session: Predicate ``session_key -> bool``. When
            ``None``, defaults to :func:`default_is_subagent_session_key`.
            Production callers wire whatever predicate matches the
            gateway's delegation convention; tests override.
        grant_id_resolver: Callable ``session_key -> grant_id | None``
            that returns the delegated-expansion grant id (or ``None``
            when no grant exists for this sub-agent session). When
            ``None``, the grant lookup is treated as inert — the
            handler still passes the sub-agent gate but the
            no-grant-error branch fires.

    Returns:
        A JSON string per the :func:`tool_result` contract.

    Tool-error payloads (returned as JSON strings):

    * Main-agent invocation: ``{"error": "lcm_expand is only available
      in sub-agent sessions..."}``.
    * Sub-agent with no grant: ``{"error": "Delegated expansion
      requires a valid grant..."}``.
    * No conversation scope: ``{"error": "No LCM conversation found
      for this session..."}``.
    * Neither summaryIds nor query: ``{"error": "Either summaryIds or
      query must be provided."}``.

    Success payloads are :func:`tool_result`-encoded dicts with the
    rendered text plus a ``details`` slice (expansion meta).
    """
    # ----- Sub-agent gate (TS lines 163-170) -------------------------------
    # The TS source resolves the effective session key from sessionKey ??
    # sessionId. We follow the same fallback so callers don't have to
    # always set both fields.
    effective_key = ""
    if isinstance(session_key, str) and session_key.strip():
        effective_key = session_key.strip()
    elif isinstance(session_id, str) and session_id.strip():
        effective_key = session_id.strip()

    predicate = (
        is_subagent_session if is_subagent_session is not None else default_is_subagent_session_key
    )
    if not predicate(effective_key):
        return tool_result({"error": _MAIN_AGENT_REFUSAL_ERROR})

    # ----- Delegated grant lookup (TS lines 171-185) -----------------------
    # ``isDelegatedSession`` in TS is the same predicate (already passed
    # above); the grant resolver returns the propagated grant id (or
    # None when no grant is held). When the resolver is not injected,
    # we treat the no-grant case as the failure mode (production wires
    # the real resolver via expansion_auth — not yet ported).
    delegated_grant_id: Optional[str] = None
    if grant_id_resolver is not None:
        delegated_grant_id = grant_id_resolver(effective_key)

    if not delegated_grant_id:
        return tool_result({"error": _NO_GRANT_ERROR})

    # ----- Param read (TS lines 153-162) -----------------------------------
    # Note: ``summaryIds`` is a list — we don't go through the string
    # helpers. ``query`` strips whitespace; empty becomes None.
    raw_summary_ids = args.get("summaryIds")
    summary_ids: list[str] = []
    if isinstance(raw_summary_ids, list):
        # Filter to non-empty strings. Defensive against a provider
        # emitting a mixed list — TS would have errored at TypeBox
        # validation, Python silently filters.
        summary_ids = [s.strip() for s in raw_summary_ids if isinstance(s, str) and s.strip()]
        # Dedupe while preserving order (TS uses array semantics; our
        # downstream orchestrator stub is also list-shaped).
        seen: set[str] = set()
        deduped: list[str] = []
        for sid in summary_ids:
            if sid not in seen:
                seen.add(sid)
                deduped.append(sid)
        summary_ids = deduped

    query = read_string_param(args, "query")

    max_depth = _read_int_or_none(args.get("maxDepth"))
    token_cap = _read_int_or_none(args.get("tokenCap"))
    if token_cap is not None and token_cap < 1:
        token_cap = 1  # match TS Math.max(1, ...) clamp at line 160
    include_messages = read_bool_param(args, "includeMessages", default=False)

    # ----- Conversation scope (TS lines 187-193) ---------------------------
    scope = resolve_lcm_conversation_scope(
        lcm=_LcmScopeAdapter(_conversation_store=ctx.conversation_store),
        params=args,
        session_id=session_id,
        session_key=session_key,
        deps=deps,
    )
    # Per the issue AC: "if no conversation resolved, error out." We mirror
    # the describe.py policy: refuse when the scope is empty (no
    # conversation_id AND not allConversations). The TS source has a more
    # complex fallthrough that allows cross-conversation grep when no
    # conversationId is resolved (TS lines 216-247); per the v0.1.0 AC we
    # tighten to require a resolved conversation_id or the explicit
    # allConversations=true flag.
    if not scope.all_conversations and scope.conversation_id is None:
        return tool_result({"error": _NO_CONVERSATION_ERROR})

    # The resolved conversation_id passed to the orchestrator. TS lines
    # 208-212 also fall through to the delegated grant's
    # ``allowedConversationIds[0]`` if there's exactly one — we omit that
    # branch here (the auth manager isn't ported; the grant resolver only
    # returns the grant_id, not the full grant record). Production will
    # widen this when expansion_auth lands.
    resolved_conversation_id = scope.conversation_id

    # ----- Query-entry path (TS lines 214-342) -----------------------------
    # TS uses ``query`` (non-empty after trim) as the discriminator. If
    # query is set, summaryIds is IGNORED — per the description string:
    # "If provided, summaryIds is ignored and the top grep results are
    # expanded."
    if query:
        try:
            if resolved_conversation_id is None:
                # The TS source allows query+no-conversation via
                # ``orchestrator.describeAndExpand``. The Python port
                # already refused above (no scope -> structured error),
                # so this branch is unreachable. Leaving the comment for
                # parity-reviewers who diff against TS lines 216-247.
                return tool_result({"error": _NO_CONVERSATION_ERROR})

            grep_result = ctx.retrieval.grep(
                query=query,
                mode="full_text",
                scope="summaries",
                conversation_id=resolved_conversation_id,
            )
            matched_summary_ids = [m.summary_id for m in grep_result.summaries]
            if not matched_summary_ids:
                # Empty grep -> empty result (TS lines 304-313).
                empty = ExpansionResult()
                return _build_success_payload(empty)

            result = ctx.orchestrator.expand(
                summary_ids=matched_summary_ids,
                conversation_id=resolved_conversation_id,
                max_depth=max_depth,
                token_cap=token_cap,
                include_messages=False,  # query path forces False per TS line 310
            )
            return _build_success_payload(result)
        except (ValueError, RuntimeError) as exc:
            # TS catches ``err`` and returns ``{error: err.message}``.
            # Python: surface ValueError / RuntimeError from the
            # orchestrator/retrieval as a tool-error payload.
            return tool_result({"error": str(exc)})

    # ----- Direct summaryIds path (TS lines 344-448) -----------------------
    if summary_ids:
        try:
            # TS lines 346-365 verify that every summary_id belongs to the
            # resolved conversation. The Python port defers this to the
            # orchestrator (which has the SQL access) — it would
            # double-query through the store otherwise.
            #
            # When resolved_conversation_id is None (all_conversations=True),
            # we pass 0 to the orchestrator like TS does at line 417
            # ("conversationId: resolvedConversationId ?? 0"). The
            # orchestrator interprets 0 as "any conversation" — this is a
            # contract pinned by the expansion.py port.
            conv_id_for_orchestrator = (
                resolved_conversation_id if resolved_conversation_id is not None else 0
            )
            result = ctx.orchestrator.expand(
                summary_ids=summary_ids,
                conversation_id=conv_id_for_orchestrator,
                max_depth=max_depth,
                token_cap=token_cap,
                include_messages=include_messages,
            )
            return _build_success_payload(result)
        except (ValueError, RuntimeError) as exc:
            return tool_result({"error": str(exc)})

    # ----- Neither shape provided (TS lines 450-452) -----------------------
    return tool_result({"error": _NEITHER_SUMMARY_IDS_NOR_QUERY_ERROR})


# ===========================================================================
# Result rendering
# ===========================================================================


def _distill_for_subagent(result: ExpansionResult) -> str:
    """Format an :class:`ExpansionResult` as a compact text payload.

    Mirrors TS ``distillForSubagent`` (``expansion.ts:221-267``). The
    output is markdown with one section per expansion entry:

    * Header: ``## Expansion Results (N summaries, M total tokens)``.
    * Per-entry: ``### sum_xxx (kind, T tokens)``,
      ``Children: sum_a, sum_b``, ``Messages: msg#N (role, T tokens)``,
      a single ``[Snippet: ...]`` if any child has content.
    * Footer: ``Cited IDs for follow-up: sum_a, sum_b``,
      ``[Truncated: yes/no]``.

    The format is deliberately verbose (markdown headers, comma-
    separated lists) because the sub-agent inlines this verbatim into
    its synthesis prompt; a tabular format would be denser but harder
    for the model to cite back.

    Args:
        result: The :class:`ExpansionResult` from the orchestrator.

    Returns:
        The rendered text. May be empty (``""``) when result.expansions
        is empty AND cited_ids is empty — defensive but the caller
        should still emit ``[Truncated: no]`` in that case.
    """
    lines: list[str] = []
    lines.append(
        f"## Expansion Results ({len(result.expansions)} summaries, "
        f"{result.total_tokens} total tokens)",
    )
    lines.append("")

    for entry in result.expansions:
        children = entry.get("children", []) or []
        messages = entry.get("messages", []) or []
        # Determine kind from children presence — TS line 231.
        kind = "condensed" if children else "leaf"
        token_sum = sum(int(c.get("tokenCount", 0) or 0) for c in children) + sum(
            int(m.get("tokenCount", 0) or 0) for m in messages
        )
        lines.append(f"### {entry.get('summaryId', '')} ({kind}, {token_sum} tokens)")

        if children:
            child_ids = ", ".join(str(c.get("summaryId", "")) for c in children)
            lines.append(f"Children: {child_ids}")

        if messages:
            msg_parts = [
                f"msg#{m.get('messageId', '')} "
                f"({m.get('role', '')}, {int(m.get('tokenCount', 0) or 0)} tokens)"
                for m in messages
            ]
            lines.append(f"Messages: {', '.join(msg_parts)}")

        # Show a single snippet (TS lines 250-255).
        for child in children:
            snippet = str(child.get("snippet", "") or "")
            if snippet:
                lines.append(f"[Snippet: {snippet}]")
                break

        lines.append("")

    if result.cited_ids:
        lines.append(f"Cited IDs for follow-up: {', '.join(result.cited_ids)}")

    lines.append(f"[Truncated: {'yes' if result.truncated else 'no'}]")

    return "\n".join(lines)


def _build_success_payload(result: ExpansionResult) -> str:
    """Render a success payload — text + details — as a JSON string.

    Mirrors the TS shape from lines 314-337 / 419-443 of the source,
    minus the deferred policy / observability / delegated keys (per
    ADR-012). The Python port emits the bare-minimum details slice:

    * ``expansionCount`` — number of expansion entries.
    * ``citedIds`` — the cited summary IDs (sub-agent quotes these).
    * ``totalTokens`` — cumulative token count.
    * ``truncated`` — whether any entry was cut.

    Args:
        result: The :class:`ExpansionResult` to render.

    Returns:
        :func:`tool_result`-encoded JSON string.
    """
    text = _distill_for_subagent(result)
    return tool_result(
        {
            "text": text,
            "expansionCount": len(result.expansions),
            "citedIds": list(result.cited_ids),
            "totalTokens": result.total_tokens,
            "truncated": result.truncated,
        },
    )


# ===========================================================================
# Numeric helpers — mirror TS Math.trunc / Math.max idioms
# ===========================================================================


def _read_int_or_none(value: Any) -> Optional[int]:
    """Coerce a numeric param to an int, or ``None`` if missing / invalid.

    Mirrors TS ``typeof p.maxDepth === "number" ? Math.trunc(p.maxDepth) :
    undefined``. Rejects bools (an int subclass in Python), NaN, +/-Inf.

    Args:
        value: The raw param value, possibly missing / None / non-numeric.

    Returns:
        Integer-truncated value, or ``None`` when the input isn't a
        valid finite number.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return int(value)
