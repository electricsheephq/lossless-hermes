"""Port of ``lcm_describe`` — LCM ID lookup with optional one-hop drilldown.

Ports ``lossless-claw/src/tools/lcm-describe-tool.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 766 LOC TS → ~640 LOC Python). The
TypeBox-declared schema lives at TS lines 61-116; the handler body at
lines 156-763. Both are translated structurally verbatim per ADR-016
(description prose byte-identical from TS source).

What this tool does
-------------------

``lcm_describe`` is the **drilldown / source-tracing** primary tool —
the PRIMARY entry point for Type E queries ("where did this synthesized
claim come from?", "show me the source leaves for this summary"). It
takes a single LCM ID — ``sum_xxx`` for a summary or ``file_xxx`` for a
stored file — and returns:

1. The base content + lineage (parent IDs, children IDs, depth, token
   counts, range, created timestamp).
2. A **subtree manifest** — every descendant in the DAG with per-node
   cost (summaries-only + with-messages) and budget-fit flags computed
   against ``resolved_token_cap`` (request param ``tokenCap`` →
   delegated-grant remaining → config default ``maxExpandTokens``).
3. **Optional one-hop expansion** of children's full content
   (``expandChildren=True``, capped at 50) and/or first-hop source
   messages for leaf summaries (``expandMessages=True``, with
   ``expandMessagesOffset`` pagination for long leaves).

The file path (``file_xxx`` IDs) emits metadata + exploration summary
with the same char-cap truncation policy.

Wave-12 F5 invariant — middleware-not-decorator
-----------------------------------------------

Per [ADR-029](../../docs/adr/029-wave-fix-provenance.md) Wave-12 F5,
:func:`handle_lcm_describe` is the **inner** handler — it must be
wrapped by ``run_with_token_gate`` middleware at the **dispatch
layer** (``LCMEngine.handle_tool_call`` per issue 06-02). The TS source
uses ``runWithTokenGate({...inner: async () => {...}})`` at lines
165-763 to funnel every return through a single tap exit, structurally
eliminating the F5 antipattern (three return paths, only two tapped).

The Python port reproduces this invariant by keeping the handler body
free of token-gate calls. The dispatch layer is responsible for the
pre-call gate (refuse if projected ratio > 0.92) and post-call tap
(account result tokens). Wrapping at registration time would freeze the
gate state to plugin-init values; the wrap MUST happen at invocation
time, hence "middleware" rather than "decorator". See the Wave-12 F5
inline comment at the call site in ``engine/__init__.py``.

This tool IS the highest blow-up-risk tool (per
``needs-compact-gate.ts:27``) — a single ``expandChildren=True
expandMessages=True expandChildrenLimit=50 expandMessagesLimit=50``
call can emit ~210K tokens before truncation. The gate's pre-call
refusal is the load-bearing protection; the in-handler
``truncate_lines_to_cap`` (per Wave-12 W1A8 #3) is the secondary
char-cap fallback.

Delegated-grant enforcement (sub-agent sessions)
------------------------------------------------

When a sub-agent session calls ``lcm_describe`` and a delegated
expansion grant is active, the handler:

1. Looks up ``remaining_token_budget`` via the grant resolver.
2. **Wave-11 reviewer P1 fix:** if base summary tokens > remaining
   budget, REDACT content BEFORE emit (don't emit-then-charge — the
   agent would already have seen the content).
3. **Wave-9 Agent #5 P1 fix:** AFTER successful emit, charges the
   grant ledger with ``base + expanded_children + expanded_messages``
   tokens — previously the side-channel expansions silently bypassed
   the grant cap.
4. **Wave-4 Auditor #9 P1 fix:** when ``resolved_token_cap == 0``
   (grant exhausted), REFUSES expansion entirely rather than emitting
   a warning and proceeding anyway.

The grant lookup uses the same pluggable
``expansion_recursion_guard._grant_resolver`` seam as 06-06; the
``expansion_auth`` module that wires the real resolver hasn't landed
yet, so the default ``None`` resolver makes the delegated path inert.
Tests can override via ``set_delegated_grant_resolver(...)`` and a
``set_grant_budget_callable(...)`` test seam (see below).

Architecture seams
------------------

The handler does NOT depend on ``LCMEngine`` directly — instead it
takes a narrow ``_DescribeContext`` Protocol that exposes:

* ``conn: sqlite3.Connection`` — for the raw queries used by the
  expansion paths (``summary_messages``, ``summary_parents``,
  ``messages``).
* ``summary_store: SummaryStore`` — for ``get_summary``,
  ``get_summary_subtree``, ``get_summary_children``,
  ``get_summary_parents``, ``get_summary_messages``,
  ``get_large_file``.
* ``conversation_store: ConversationStore`` — for the conversation
  scope resolver via the ``_LcmLike`` shape.
* ``timezone: str`` — passed through to the timestamp formatter.
* ``max_expand_tokens: int`` — the default budget cap when no
  ``tokenCap`` request param and no delegated grant exists.

This lets the test suite construct a minimal context dict without
spinning up the full :class:`LCMEngine`, and lets the eventual 06-02
dispatch wrap pass the engine seam in.

References
----------

* TS source: ``lossless-claw/src/tools/lcm-describe-tool.ts`` (766 LOC).
* Porting guide: ``docs/porting-guides/tools.md`` §"lcm_describe"
  (lines 132-198).
* Issue spec: ``epics/06-tools/06-07-lcm-describe.md``.
* [ADR-016](../../docs/adr/016-typebox-translation.md) — TypeBox
  hand-translate policy (description prose byte-identical).
* [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-12 F5
  (middleware-not-decorator), Wave-12 N3 (truncation regex pin).
* TS test fixture: ``test/lcm-describe-expand-flags.test.ts`` (415 LOC).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Final, Optional, Protocol

from lossless_hermes.plugin.result_budget import (
    MAX_RESULT_CHARS,
    truncation_notice,
)
from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.store.summary import SummaryStore
from lossless_hermes.tools import TOOL_SCHEMAS
from lossless_hermes.tools._common import (
    read_bool_param,
    read_string_param,
    tool_result,
)
from lossless_hermes.tools._typebox import (
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
    "LCM_DESCRIBE_DESCRIPTION",
    "LCM_DESCRIBE_SCHEMA",
    "DescribeContext",
    "handle_lcm_describe",
    "set_grant_budget_lookup",
)


# ===========================================================================
# Schema — verbatim from TS source (ADR-016 §Consequences)
# ===========================================================================
#
# Description prose is byte-identical to lcm-describe-tool.ts lines
# 146-155 (the `description:` block) and the per-field `description`
# strings at lines 61-116. The mechanical TypeBox → dict translation
# uses the helpers in `_typebox.py`.

LCM_DESCRIBE_DESCRIPTION: Final[str] = (
    "Look up an LCM item by ID, with optional one-hop drilldown. "
    "PRIMARY tool for Type E queries (drilldown / source-tracing): "
    "'where did this synthesized claim come from?', 'show me the source leaves "
    "for this summary'. Set expandChildren=true to inline child summaries "
    "(capped 20, max 50) and/or expandMessages=true to inline raw source "
    "messages. Inspects summaries (sum_xxx) or stored files (file_xxx). "
    "For multi-hop drilldown that needs to read more than one level, "
    "use lcm_expand_query (delegated sub-agent expansion). "
    "Returns summary content, lineage, token counts, file exploration, "
    "and (with expand flags) one-hop child/message detail."
)
"""Verbatim from ``lcm-describe-tool.ts:146-155``. Per ADR-016 §Consequences
this is the load-bearing model-facing prose that drives tool selection."""


LCM_DESCRIBE_SCHEMA: Final[dict[str, Any]] = tool_schema(
    name="lcm_describe",
    description=LCM_DESCRIBE_DESCRIPTION,
    parameters=object_schema(
        id=string_field(
            "The LCM ID to look up. Use sum_xxx for summaries, file_xxx for files.",
        ),
        conversationId=optional(
            number_field(
                "Physical conversation ID to scope describe lookups to. If omitted, uses the current session family.",
            ),
        ),
        allConversations=optional(
            boolean_field(
                "Set true to explicitly allow lookups across all conversations. Ignored when conversationId is provided.",
            ),
        ),
        tokenCap=optional(
            number_field(
                "Optional budget cap used for subtree manifest budget-fit annotations.",
                minimum=1,
            ),
        ),
        expandChildren=optional(
            boolean_field(
                "When true (and target is a sum_xxx), include the first-hop child summaries' full content inline (capped at expandChildrenLimit, default 5). For deeper / wider expansion use the sub-agent lcm_expand_query path. Ignored for file_xxx targets.",
            ),
        ),
        expandChildrenLimit=optional(
            number_field(
                "Max child summaries to inline when expandChildren=true (default 20, max 50).",
                minimum=1,
                maximum=50,
            ),
        ),
        expandMessages=optional(
            boolean_field(
                "When true (and target is a sum_xxx leaf), include the first-hop source messages' full verbatim content inline (capped at expandMessagesLimit, default 20). Ignored for condensed summaries (no direct messages) and file_xxx targets. Suppressed messages are filtered out.",
            ),
        ),
        expandMessagesLimit=optional(
            number_field(
                "Max source messages to inline when expandMessages=true (default 20, max 50).",
                minimum=1,
                maximum=50,
            ),
        ),
        expandMessagesOffset=optional(
            number_field(
                "Skip the first N messages before returning expandMessagesLimit. Use to paginate through long leaves (e.g. 216-message leaves where the default 20 only covers ~10% of source). Default 0.",
                minimum=0,
            ),
        ),
    ),
)
"""OpenAI-function-call schema for ``lcm_describe``. Verbatim translation
of the TypeBox declaration at ``lcm-describe-tool.ts:61-116`` per
ADR-016."""


# Register at module import time per the TOOL_SCHEMAS contract documented
# in tools/__init__.py. The 06-02 dispatch table reads via
# ``get_tool_schemas()`` so this side-effect is what makes the tool
# discoverable to the LCMEngine.
TOOL_SCHEMAS.append(LCM_DESCRIBE_SCHEMA)


# ===========================================================================
# Constants and limits
# ===========================================================================

_DEFAULT_EXPAND_CHILDREN_LIMIT: Final[int] = 20
"""Default ``expandChildrenLimit`` when the caller omits it (TS line 463)."""

_MAX_EXPAND_CHILDREN_LIMIT: Final[int] = 50
"""Hard cap on ``expandChildrenLimit`` regardless of caller's value (TS line 462)."""

_DEFAULT_EXPAND_MESSAGES_LIMIT: Final[int] = 20
"""Default ``expandMessagesLimit`` when the caller omits it (TS line 556)."""

_MAX_EXPAND_MESSAGES_LIMIT: Final[int] = 50
"""Hard cap on ``expandMessagesLimit`` regardless of caller's value (TS line 555)."""

_EXPAND_MESSAGES_OFFSET_HARD_CAP: Final[int] = 100_000
"""Hard cap on ``expandMessagesOffset`` — clamp to stop runaway / adversarial
agents from triggering full-table scans via LIMIT/OFFSET. 100k is well
past any realistic leaf size (max observed: 216) and stops a runaway
loop from costing seconds-per-call. Audit 2 finding #4 (TS line 562)."""


# ===========================================================================
# Test seams — grant-budget lookup
# ===========================================================================
#
# The TS source calls ``getRuntimeExpansionAuthManager().getRemainingTokenBudget(grantId)``
# and ``consumeTokenBudget(grantId, tokens)``. The Python ``expansion_auth``
# module that exposes the real auth manager has NOT landed yet (epic 06;
# this issue covers the describe tool only). Until then we expose a
# pluggable lookup callable that tests can swap in:
#
#   - ``get_remaining_token_budget(grant_id) -> int | None``
#   - ``consume_token_budget(grant_id, tokens) -> None``
#
# The default no-op makes the delegated-grant path inert in production
# until ``expansion_auth`` lands. Per the test seam in
# ``expansion_recursion_guard.set_delegated_grant_resolver``, both knobs
# are settable at runtime.

_GrantBudgetLookup = Callable[[str], Optional[int]]
_GrantBudgetConsumer = Callable[[str, int], None]


def _default_grant_budget_lookup(grant_id: str) -> Optional[int]:
    """Default lookup — returns ``None`` (no grant budget tracked).

    The real implementation lands with the ``expansion_auth`` module;
    until then this is a no-op so production calls behave as if the
    session is non-delegated.
    """
    del grant_id
    return None


def _default_grant_budget_consumer(grant_id: str, tokens: int) -> None:
    """Default consumer — no-op.

    See :func:`_default_grant_budget_lookup` — same rationale.
    """
    del grant_id, tokens
    return None


_grant_budget_lookup: _GrantBudgetLookup = _default_grant_budget_lookup
_grant_budget_consumer: _GrantBudgetConsumer = _default_grant_budget_consumer


def set_grant_budget_lookup(
    *,
    lookup: Optional[_GrantBudgetLookup] = None,
    consumer: Optional[_GrantBudgetConsumer] = None,
) -> None:
    """Register custom grant-budget lookup / consumer callables.

    Used by the ``expansion_auth`` module (when it lands) to wire the
    real auth manager. Tests can pass stubs for the delegated-grant
    redaction / consumption tests. Passing ``None`` for a slot resets
    that slot to its no-op default.

    Args:
        lookup: Callable ``grant_id -> remaining_tokens | None``.
            Returns the integer remaining-token budget for the given
            grant_id, or ``None`` when no grant exists. ``None`` resets
            to the no-op default.
        consumer: Callable ``(grant_id, tokens) -> None``. Decrements
            the grant's ledger by ``tokens`` after a successful emit.
            ``None`` resets to the no-op default.

    Thread-safety: plain attribute assignment is atomic in CPython for
    module-level globals; no lock needed. Tests are responsible for
    resetting between cases (use a pytest fixture with
    ``set_grant_budget_lookup(lookup=None, consumer=None)`` teardown).
    """
    global _grant_budget_lookup, _grant_budget_consumer
    if lookup is not None:
        _grant_budget_lookup = lookup
    else:
        _grant_budget_lookup = _default_grant_budget_lookup
    if consumer is not None:
        _grant_budget_consumer = consumer
    else:
        _grant_budget_consumer = _default_grant_budget_consumer


# ===========================================================================
# DescribeContext — narrow Protocol exposing what the handler needs
# ===========================================================================


class DescribeContext(Protocol):
    """The handler's collaborator surface.

    Mirrors the slice of :class:`~lossless_hermes.engine.LCMEngine` that
    ``lcm_describe`` actually needs. Using a structural Protocol keeps
    the handler decoupled from the engine class shape and lets tests
    construct a tiny stand-in dataclass.

    Required attributes:

    * ``conn``: :class:`sqlite3.Connection` for the SQL the handler
      runs directly (the ``COUNT(*) FROM summary_parents`` raw-child
      probe, the source-message expansion query).
    * ``summary_store``: :class:`SummaryStore` for the higher-level
      summary / subtree / file lookups.
    * ``conversation_store``: :class:`ConversationStore` — for the
      conversation-scope resolver via the ``_LcmLike`` Protocol shape.
    * ``timezone``: IANA timezone name for the timestamp formatter
      (e.g. ``"UTC"``, ``"America/Los_Angeles"``).
    * ``max_expand_tokens``: int — the default budget cap when no
      ``tokenCap`` request param AND no delegated grant exists.
      Production callers wire this from ``LcmConfig.maxExpandTokens``.
    """

    conn: sqlite3.Connection
    summary_store: SummaryStore
    conversation_store: ConversationStore
    timezone: str
    max_expand_tokens: int


@dataclass
class _LcmScopeAdapter:
    """Adapter that satisfies :class:`~..conversation_scope._LcmLike`.

    The conversation-scope resolver consumes a ``_LcmLike`` protocol —
    anything with a ``_conversation_store`` attribute. We don't expose
    a private attribute on :class:`DescribeContext`, so adapt at the
    call site.

    The field type is ``Optional[ConversationStore]`` to byte-match the
    Protocol's declaration (``_LcmLike._conversation_store: Optional[...]``)
    — ty's structural matching otherwise sees a narrower type and rejects
    the substitution. In practice the adapter is always constructed
    with a non-None store at the call site in :func:`handle_lcm_describe`.
    """

    _conversation_store: Optional[ConversationStore]


# ===========================================================================
# Handler entry point
# ===========================================================================


def handle_lcm_describe(
    args: dict[str, Any],
    *,
    ctx: DescribeContext,
    deps: LcmDependencies,
    session_key: Optional[str] = None,
    session_id: Optional[str] = None,
    is_subagent_session: Optional[Callable[[str], bool]] = None,
    grant_id_resolver: Optional[Callable[[str], Optional[str]]] = None,
) -> str:
    """Handle an ``lcm_describe`` tool call.

    **Wave-12 F5 invariant:** this is the INNER handler. The
    ``run_with_token_gate`` middleware MUST wrap this call at the
    dispatch layer (issue 06-02 — ``LCMEngine.handle_tool_call``); see
    the module docstring's "Wave-12 F5" section. The wrap MUST happen
    at invocation time, NOT at registration time (decorator-time
    computation would freeze the gate state).

    Args:
        args: The tool-call ``arguments`` dict from the LLM provider.
            Read defensively — see :mod:`lossless_hermes.tools._common`.
        ctx: A :class:`DescribeContext` exposing the SQL / store /
            timezone collaborator surface.
        deps: :class:`LcmDependencies` slice (the same dataclass
            ``resolve_lcm_conversation_scope`` consumes).
        session_key: Optional cross-conversation session-family key. If
            omitted, the handler falls through to ``session_id`` for
            scope resolution.
        session_id: Optional runtime session id. Either ``session_key``
            or ``session_id`` should be supplied so the scope resolver
            can find an anchor conversation.
        is_subagent_session: Predicate ``session_key -> bool`` that
            returns True for delegated sub-agent sessions. Defaults to
            "never a sub-agent" (so the delegated-grant path is inert).
        grant_id_resolver: Callable ``session_key -> grant_id | None``.
            Returns the delegated-expansion grant id for a sub-agent
            session, or ``None`` when no grant exists. The real
            resolver wires through ``expansion_auth`` (not yet ported).

    Returns:
        A JSON string per the :func:`tool_result` contract — Hermes's
        :py:meth:`ContextEngine.handle_tool_call` consumes JSON
        strings, not structured dicts. The wrap layer at 06-02 may
        re-encode for the eventual ``{content, details}`` shape, but
        the handler itself returns JSON.

    Tool-error payloads (returned as JSON strings):

    * No conversation scope: ``{"error": "No LCM conversation found..."}``.
    * Not found: ``{"error": "Not found: <id>", "hint": "..."}``.
    * Found-but-outside-scope: ``{"error": "Not found in this session
      scope: <id>", "hint": "Use allConversations=true..."}``.

    Success payloads are :func:`tool_result`-encoded dicts with the
    rendered text plus a ``details`` slice (manifest + expansion meta).
    """
    # ----- Param read + scope resolve --------------------------------------
    # ``id`` is required — strip whitespace per TS line 178.
    item_id = read_string_param(args, "id", required=True)
    assert item_id is not None  # required=True guarantees non-None

    scope = resolve_lcm_conversation_scope(
        lcm=_LcmScopeAdapter(_conversation_store=ctx.conversation_store),
        params=args,
        session_id=session_id,
        session_key=session_key,
        deps=deps,
    )
    if not scope.all_conversations and scope.conversation_id is None:
        return tool_result(
            {
                "error": (
                    "No LCM conversation found for this session. "
                    "Provide conversationId or set allConversations=true."
                ),
            },
        )

    # ----- Dispatch to summary vs file lookup ------------------------------
    # TS calls retrieval.describe(id) which probes both summaries and
    # large_files in one call. We split via _resolve_target() because the
    # store-level API exposes the two paths separately. The semantics
    # match: summary first (the common case), then file.
    target = _resolve_target(item_id, store=ctx.summary_store)
    if target is None:
        return tool_result(
            {
                "error": f"Not found: {item_id}",
                "hint": "Check the ID format (sum_xxx for summaries, file_xxx for files).",
            },
        )

    # ----- Conversation-scope enforcement ----------------------------------
    # TS lines 201-217: when we resolved a specific conversation_id (i.e.
    # not allConversations), verify the target's conversation_id is in
    # the allowed set. Items found in OTHER conversations report as
    # "not in scope" so the agent has a path forward.
    if scope.conversation_id is not None:
        item_conv_id = target.conversation_id
        allowed = set(
            scope.conversation_ids if scope.conversation_ids else [scope.conversation_id],
        )
        if item_conv_id is not None and item_conv_id not in allowed:
            return tool_result(
                {
                    "error": f"Not found in this session scope: {item_id}",
                    "hint": "Use allConversations=true for cross-conversation lookup.",
                },
            )

    # ----- Branch on target kind -------------------------------------------
    # isinstance check (over kind-string comparison) so the type
    # narrowing makes ``target.summary`` / ``target.file_`` typed.
    if isinstance(target, _SummaryTarget):
        return _emit_summary(
            args=args,
            item_id=item_id,
            summary=target.summary,
            subtree=ctx.summary_store.get_summary_subtree(item_id),
            ctx=ctx,
            session_key=session_key,
            session_id=session_id,
            is_subagent_session=is_subagent_session,
            grant_id_resolver=grant_id_resolver,
        )
    return _emit_file(item_id=item_id, file_=target.file_, timezone_name=ctx.timezone)


# ===========================================================================
# Target resolution — summary vs file
# ===========================================================================


@dataclass(frozen=True)
class _SummaryTarget:
    """Result of probing summaries for ``item_id`` (mirrors TS ``{type: "summary", summary}``)."""

    summary: Any  # SummaryRecord — typed Any to dodge cyclic import noise
    kind: str = "summary"

    @property
    def conversation_id(self) -> Optional[int]:
        return getattr(self.summary, "conversation_id", None)


@dataclass(frozen=True)
class _FileTarget:
    """Result of probing large_files for ``item_id`` (mirrors TS ``{type: "file", file}``)."""

    file_: Any  # LargeFileRecord
    kind: str = "file"

    @property
    def conversation_id(self) -> Optional[int]:
        return getattr(self.file_, "conversation_id", None)


_Target = _SummaryTarget | _FileTarget


def _resolve_target(item_id: str, *, store: SummaryStore) -> Optional[_Target]:
    """Probe ``summaries`` first, then ``large_files``. Mirrors TS retrieval.describe.

    Order matches the TS retrieval.describe which queries summaries by
    summary_id, then large_files by file_id, and emits the FIRST match.
    Tests pass IDs of either kind without knowing in advance.
    """
    summary = store.get_summary(item_id)
    if summary is not None:
        return _SummaryTarget(summary=summary)
    file_ = store.get_large_file(item_id)
    if file_ is not None:
        return _FileTarget(file_=file_)
    return None


# ===========================================================================
# Summary path (TS lines 219-727)
# ===========================================================================


def _emit_summary(  # noqa: PLR0912, PLR0915 — mirrors TS structure; splitting would hide control flow
    *,
    args: dict[str, Any],
    item_id: str,
    summary: Any,
    subtree: list[Any],
    ctx: DescribeContext,
    session_key: Optional[str],
    session_id: Optional[str],
    is_subagent_session: Optional[Callable[[str], bool]],
    grant_id_resolver: Optional[Callable[[str], Optional[str]]],
) -> str:
    """Render the summary path output. Mirrors TS lines 219-727."""
    timezone_name = ctx.timezone

    # ----- Resolve token cap and delegated-grant state ---------------------
    requested_token_cap = _normalize_requested_token_cap(args.get("tokenCap"))

    delegated_grant_id = ""
    delegated_remaining_budget: Optional[int] = None
    # session_key is normalized — TS at 222-223 uses sessionKey ?? sessionId
    effective_key = ""
    if isinstance(session_key, str) and session_key.strip():
        effective_key = session_key.strip()
    elif isinstance(session_id, str) and session_id.strip():
        effective_key = session_id.strip()

    if (
        effective_key
        and is_subagent_session is not None
        and is_subagent_session(effective_key)
        and grant_id_resolver is not None
    ):
        resolved_grant = grant_id_resolver(effective_key)
        delegated_grant_id = resolved_grant or ""
        if delegated_grant_id:
            delegated_remaining_budget = _grant_budget_lookup(delegated_grant_id)

    default_token_cap = max(1, int(ctx.max_expand_tokens))

    # TS lines 232-240: pick base = request → grant → default; clamp to grant
    # if grant exists; floor at 1 if no grant.
    if requested_token_cap is not None:
        base_cap = requested_token_cap
    elif delegated_remaining_budget is not None:
        base_cap = delegated_remaining_budget
    else:
        base_cap = default_token_cap
    if delegated_remaining_budget is not None:
        resolved_token_cap = max(0, min(base_cap, delegated_remaining_budget))
    else:
        resolved_token_cap = max(1, base_cap)

    # Budget-source label for the manifest details (TS line 711)
    if requested_token_cap is not None:
        budget_source = "request"
    elif delegated_remaining_budget is not None:
        budget_source = "delegated_grant_remaining"
    else:
        budget_source = "config_default"

    # ----- Build manifest from subtree (TS lines 242-268) -------------------
    manifest_nodes: list[dict[str, Any]] = []
    for node in subtree:
        # Each subtree entry exposes the same fields as SummarySubtreeNodeRecord
        node_token_count = max(0, int(getattr(node, "token_count", 0) or 0))
        node_desc_token = max(0, int(getattr(node, "descendant_token_count", 0) or 0))
        node_src_msg_token = max(
            0,
            int(getattr(node, "source_message_token_count", 0) or 0),
        )
        summaries_only_cost = max(0, node_token_count + node_desc_token)
        with_messages_cost = max(0, summaries_only_cost + node_src_msg_token)
        manifest_nodes.append(
            {
                "summaryId": getattr(node, "summary_id", ""),
                "parentSummaryId": getattr(node, "parent_summary_id", None),
                "depthFromRoot": int(getattr(node, "depth_from_root", 0) or 0),
                "depth": int(getattr(node, "depth", 0) or 0),
                "kind": getattr(node, "kind", ""),
                "tokenCount": node_token_count,
                "descendantCount": int(getattr(node, "descendant_count", 0) or 0),
                "descendantTokenCount": node_desc_token,
                "sourceMessageTokenCount": node_src_msg_token,
                "childCount": int(getattr(node, "child_count", 0) or 0),
                "earliestAt": getattr(node, "earliest_at", None),
                "latestAt": getattr(node, "latest_at", None),
                "path": getattr(node, "path", ""),
                "costs": {
                    "summariesOnly": summaries_only_cost,
                    "withMessages": with_messages_cost,
                },
                "budgetFit": {
                    "summariesOnly": summaries_only_cost <= resolved_token_cap,
                    "withMessages": with_messages_cost <= resolved_token_cap,
                },
            },
        )

    # ----- Render header / meta / lineage (TS lines 270-292) ---------------
    lines: list[str] = []
    lines.append(f"LCM_SUMMARY {item_id}")
    parent_records = ctx.summary_store.get_summary_parents(item_id)
    parent_ids = [
        getattr(r, "summary_id", "") for r in parent_records if getattr(r, "summary_id", "")
    ]
    child_records = ctx.summary_store.get_summary_children(item_id)
    child_ids = [
        getattr(r, "summary_id", "") for r in child_records if getattr(r, "summary_id", "")
    ]
    session_key_for_meta = getattr(summary, "session_key", "") or "-"
    s_kind = getattr(summary, "kind", "")
    s_depth = int(getattr(summary, "depth", 0) or 0)
    s_token_count = int(getattr(summary, "token_count", 0) or 0)
    s_desc_token = int(getattr(summary, "descendant_token_count", 0) or 0)
    s_src_msg_token = int(getattr(summary, "source_message_token_count", 0) or 0)
    s_desc_count = int(getattr(summary, "descendant_count", 0) or 0)
    s_earliest = getattr(summary, "earliest_at", None)
    s_latest = getattr(summary, "latest_at", None)
    s_created = getattr(summary, "created_at", None)
    s_conv_id = getattr(summary, "conversation_id", 0) or 0
    lines.append(
        f"meta conv={s_conv_id} sessionKey={session_key_for_meta} kind={s_kind} "
        f"depth={s_depth} tok={s_token_count} "
        f"descTok={s_desc_token} srcTok={s_src_msg_token} "
        f"desc={s_desc_count} "
        f"range={_format_display_time(s_earliest, timezone_name)}.."
        f"{_format_display_time(s_latest, timezone_name)} "
        f"created={_format_display_time(s_created, timezone_name)} "
        f"budgetCap={resolved_token_cap}",
    )
    # Wave-1 Auditor #5 finding #3: surface the exhaustion explicitly so
    # readers don't see "budget=over" everywhere with no explanation.
    # LCM Wave-1 (2025-11): delegated-grant exhaustion → explicit signal line.
    # Original: lossless-claw/src/tools/lcm-describe-tool.ts:282.
    if resolved_token_cap == 0 and delegated_remaining_budget is not None:
        lines.append(
            "budget exhausted: delegated grant has 0 tokens remaining; "
            "expansion is blocked. Re-issue the grant via lcm_expand_query "
            "with a higher remainingTokens before drilling further.",
        )
    if parent_ids:
        lines.append(f"parents {' '.join(parent_ids)}")
    if child_ids:
        lines.append(f"children {' '.join(child_ids)}")
    lines.append("manifest")
    for node in manifest_nodes:
        lines.append(
            f"d{node['depthFromRoot']} {node['summaryId']} k={node['kind']} "
            f"tok={node['tokenCount']} "
            f"descTok={node['descendantTokenCount']} "
            f"srcTok={node['sourceMessageTokenCount']} "
            f"desc={node['descendantCount']} child={node['childCount']} "
            f"range={_format_display_time(node['earliestAt'], timezone_name)}.."
            f"{_format_display_time(node['latestAt'], timezone_name)} "
            f"cost[s={node['costs']['summariesOnly']},"
            f"m={node['costs']['withMessages']}] "
            f"budget[s={'in' if node['budgetFit']['summariesOnly'] else 'over'},"
            f"m={'in' if node['budgetFit']['withMessages'] else 'over'}]",
        )

    # ----- Header expansion signal (TS lines 305-351) ----------------------
    expand_children = read_bool_param(args, "expandChildren")
    expand_messages = read_bool_param(args, "expandMessages")

    if expand_children:
        raw_child_count = _count_raw_children(item_id, conn=ctx.conn)
        if raw_child_count == 0:
            lines.append("expansion (children): 0 — terminal node, nothing to drill into")
        elif len(child_ids) == 0 and raw_child_count > 0:
            lines.append(
                f"expansion (children): 0 of {raw_child_count} raw — "
                "ALL children suppressed; details below",
            )
        else:
            suppressed_count = raw_child_count - len(child_ids)
            supp_note = (
                f" ({suppressed_count} suppressed and filtered)" if suppressed_count > 0 else ""
            )
            lines.append(
                f"expansion (children): {len(child_ids)} of {raw_child_count} raw"
                f"{supp_note}; details below",
            )
    if expand_messages and s_kind != "leaf":
        lines.append("expansion (messages): n/a — target is not a leaf")

    # ----- Content emit or redacted (TS lines 360-373) ---------------------
    # LCM Wave-11 (2026-04): redact base content BEFORE emit when delegated
    # grant remaining < base token count. Emitting-then-charging would let
    # the agent see content even after accounting refused.
    # Original: lossless-claw/src/tools/lcm-describe-tool.ts:360-373.
    s_content = getattr(summary, "content", "") or ""
    base_summary_tokens = s_token_count
    is_delegated_and_over_budget = (
        delegated_grant_id != ""
        and delegated_remaining_budget is not None
        and delegated_remaining_budget < base_summary_tokens
    )
    if is_delegated_and_over_budget:
        lines.append("content")
        lines.append(
            f"[REDACTED — base summary content is {base_summary_tokens} tokens "
            f"but the delegated grant has only {delegated_remaining_budget} "
            "tokens remaining. Re-issue the grant with a larger "
            "remainingTokens via lcm_expand_query, or call from a "
            "non-delegated session.]",
        )
    else:
        lines.append("content")
        lines.append(s_content)

    # ----- expandChildren detail (TS lines 380-523) ------------------------
    expanded_children: list[dict[str, Any]] = []
    expanded_messages: list[dict[str, Any]] = []

    expand_children_status: Optional[str] = None
    # LCM Wave-4 (2026-01): when delegated-grant budget is exhausted, REFUSE
    # to expand AT ALL rather than emitting a warning and silently
    # expanding anyway. Wave-8 P1 added distinct "budget-exhausted" status.
    # Original: lossless-claw/src/tools/lcm-describe-tool.ts:411-424.
    budget_exhausted = resolved_token_cap == 0 and delegated_remaining_budget is not None

    if expand_children and budget_exhausted:
        expand_children_status = "budget-exhausted"
        lines.append("")
        lines.append(
            "expanded children: SKIPPED — delegated grant has 0 tokens "
            "remaining; expansion blocked. Re-issue the grant via "
            "lcm_expand_query with a higher remainingTokens to unblock.",
        )
    elif expand_children:
        # Re-query raw child count (TS lines 434-445) — getSummaryChildren
        # already suppression-filters, so an empty child_ids could mean
        # "no children" OR "all suppressed". Distinguish.
        raw_child_count = _count_raw_children(item_id, conn=ctx.conn)
        if len(child_ids) == 0 and raw_child_count == 0:
            expand_children_status = "no-children"
            lines.append("")
            lines.append(
                "expanded children: 0 (this node has no children — "
                "it is a terminal in the DAG; nothing to drill into)",
            )
        elif len(child_ids) == 0 and raw_child_count > 0:
            expand_children_status = "all-suppressed"
            lines.append("")
            lines.append(
                f"expanded children: 0/{raw_child_count} (this node has "
                f"{raw_child_count} children but ALL are suppressed — "
                "they exist in the DAG but have been removed from the "
                "agent surface)",
            )
        else:
            requested_limit = _clamp_int(
                args.get("expandChildrenLimit"),
                default=_DEFAULT_EXPAND_CHILDREN_LIMIT,
                minimum=1,
                maximum=_MAX_EXPAND_CHILDREN_LIMIT,
            )
            ids = child_ids[:requested_limit]
            # Re-query each child's content + token count via raw SQL —
            # TS lines 466-481. The summary_store.get_summary path would
            # be too many round-trips; one IN query is what TS uses.
            rows = _fetch_children_rows(ids, conn=ctx.conn)
            for r in rows:
                expanded_children.append(
                    {
                        "summaryId": r["summary_id"],
                        "kind": r["kind"],
                        "tokenCount": int(r["token_count"] or 0),
                        "createdAt": r["created_at"],
                        "content": r["content"] or "",
                    },
                )
            requested_count = len(ids)
            survived = len(expanded_children)
            total_children = len(child_ids)
            was_capped = total_children > requested_limit
            if survived == 0:
                expand_children_status = "all-suppressed"
                lines.append("")
                lines.append(
                    f"expanded children: 0/{total_children} (all children "
                    "are suppressed — none returned; the node has children "
                    "but they have been removed from the agent surface)",
                )
            else:
                expand_children_status = "capped" if was_capped else "ok"
                lines.append("")
                suffix = (
                    f" ({requested_count - survived} children suppressed and filtered out)"
                    if survived < requested_count
                    else ""
                )
                cap_note = (
                    f" (capped at limit={requested_limit}; raise expandChildrenLimit "
                    f"up to {_MAX_EXPAND_CHILDREN_LIMIT} for more)"
                    if was_capped
                    else ""
                )
                lines.append(
                    f"expanded children: {survived}/{total_children}{cap_note}{suffix}",
                )
                for child in expanded_children:
                    lines.append("")
                    lines.append(
                        f"### child {child['summaryId']} ({child['kind']}, "
                        f"{child['tokenCount']} tokens, "
                        f"{_format_display_time(child['createdAt'], timezone_name)})",
                    )
                    lines.append("")
                    lines.append(child["content"])

    # ----- expandMessages detail (TS lines 525-655) ------------------------
    expand_messages_status: Optional[str] = None
    if expand_messages and budget_exhausted:
        expand_messages_status = "budget-exhausted"
        lines.append("")
        lines.append(
            "expanded source messages: SKIPPED — delegated grant has 0 "
            "tokens remaining; expansion blocked. Re-issue the grant via "
            "lcm_expand_query with a higher remainingTokens to unblock.",
        )
    elif expand_messages:
        if s_kind != "leaf":
            expand_messages_status = "not-leaf"
            lines.append("")
            lines.append(
                f"expanded source messages: 0 (target is a {s_kind} summary, "
                "not a leaf — condensed summaries don't have direct messages; "
                "expand its children first to find leaves)",
            )
        else:
            requested_limit = _clamp_int(
                args.get("expandMessagesLimit"),
                default=_DEFAULT_EXPAND_MESSAGES_LIMIT,
                minimum=1,
                maximum=_MAX_EXPAND_MESSAGES_LIMIT,
            )
            # LCM Wave-3 (2026-01): clamp offset upper-bound to
            # _EXPAND_MESSAGES_OFFSET_HARD_CAP so adversarial / runaway
            # agents can't trigger LIMIT/OFFSET full-table scans.
            # Original: lossless-claw/src/tools/lcm-describe-tool.ts:562-565.
            requested_offset = _clamp_int(
                args.get("expandMessagesOffset"),
                default=0,
                minimum=0,
                maximum=_EXPAND_MESSAGES_OFFSET_HARD_CAP,
            )
            total_messages = _count_source_messages(item_id, conn=ctx.conn)
            rows = _fetch_source_messages(
                item_id,
                limit=requested_limit,
                offset=requested_offset,
                conn=ctx.conn,
            )
            for r in rows:
                expanded_messages.append(
                    {
                        "messageId": int(r["message_id"]),
                        "role": r["role"],
                        "tokenCount": int(r["token_count"] or 0),
                        "createdAt": r["created_at"],
                        "content": r["content"] or "",
                    },
                )
            if total_messages == 0:
                expand_messages_status = "no-messages"
                lines.append("")
                lines.append(
                    "expanded source messages: 0 (this leaf has no "
                    "associated messages — likely a synthetic / migrated "
                    "leaf without source-message lineage)",
                )
            elif len(expanded_messages) == 0:
                # Either offset went past the end or all in-range messages
                # were suppressed. Audit 2 finding #6 → distinct status
                # for offset-past-end so callers don't read "ok" + 0
                # results and conclude the leaf is empty.
                expand_messages_status = (
                    "offset-past-end" if requested_offset >= total_messages else "all-suppressed"
                )
                lines.append("")
                if requested_offset >= total_messages:
                    lines.append(
                        f"expanded source messages: 0/{total_messages} "
                        f"(offset={requested_offset} is past the end; "
                        "reduce offset to see content)",
                    )
                else:
                    lines.append(
                        f"expanded source messages: 0/{total_messages} "
                        "(all messages in this offset window were "
                        "suppressed and filtered out)",
                    )
            else:
                remaining = total_messages - (requested_offset + len(expanded_messages))
                expand_messages_status = "capped" if remaining > 0 else "ok"
                lines.append("")
                range_label = (
                    f"[{requested_offset + 1}..{requested_offset + len(expanded_messages)}]"
                )
                pagination_hint = (
                    f" — {remaining} more after this window; paginate with "
                    f"expandMessagesOffset={requested_offset + len(expanded_messages)}"
                    if remaining > 0
                    else ""
                )
                lines.append(
                    f"expanded source messages: {len(expanded_messages)}/"
                    f"{total_messages} {range_label}{pagination_hint}",
                )
                for msg in expanded_messages:
                    lines.append("")
                    lines.append(
                        f"### msg#{msg['messageId']} ({msg['role']}, "
                        f"{msg['tokenCount']} tokens, "
                        f"{_format_display_time(msg['createdAt'], timezone_name)})",
                    )
                    lines.append("")
                    lines.append(msg["content"])

    # ----- Grant ledger consumption (TS lines 668-690) ---------------------
    # LCM Wave-9 (2026-03): expansions previously bypassed the grant cap.
    # Now we sum base + expansions and consume from the grant after a
    # successful emit. If base was REDACTED (Wave-11 above), charge 0 for
    # it — the agent didn't actually see it.
    # Original: lossless-claw/src/tools/lcm-describe-tool.ts:668-690.
    if delegated_grant_id != "":
        base_tokens = 0 if is_delegated_and_over_budget else base_summary_tokens
        expanded_children_tokens = sum(c["tokenCount"] for c in expanded_children)
        expanded_messages_tokens = sum(m["tokenCount"] for m in expanded_messages)
        consumed = base_tokens + expanded_children_tokens + expanded_messages_tokens
        if consumed > 0:
            _grant_budget_consumer(delegated_grant_id, consumed)

    # ----- Truncate to MAX_RESULT_CHARS (TS lines 692-696) -----------------
    trimmed_text, truncated = _truncate_lines_to_cap(
        lines,
        reason_hint=("lower expandChildrenLimit / expandMessagesLimit, or request a narrower id"),
    )

    payload: dict[str, Any] = {
        "type": "summary",
        "text": trimmed_text,
        "truncated": truncated,
        "manifest": {
            "tokenCap": resolved_token_cap,
            "budgetSource": budget_source,
            "nodes": manifest_nodes,
            "truncated": truncated,
        },
        "expansion": {
            "children": expanded_children,
            "childrenStatus": expand_children_status,
            "messages": expanded_messages,
            "messagesStatus": expand_messages_status,
        },
    }
    return tool_result(payload)


# ===========================================================================
# File path (TS lines 729-758)
# ===========================================================================


def _emit_file(*, item_id: str, file_: Any, timezone_name: str) -> str:
    """Render the file path output. Mirrors TS lines 729-758."""
    lines: list[str] = []
    lines.append(f"## LCM File: {item_id}")
    lines.append("")
    lines.append(f"**Conversation:** {getattr(file_, 'conversation_id', '')}")
    file_name = getattr(file_, "file_name", None) or "(no name)"
    lines.append(f"**Name:** {file_name}")
    mime_type = getattr(file_, "mime_type", None) or "unknown"
    lines.append(f"**Type:** {mime_type}")
    byte_size = getattr(file_, "byte_size", None)
    if byte_size is not None:
        lines.append(f"**Size:** {byte_size:,} bytes")
    created_at = getattr(file_, "created_at", None)
    lines.append(f"**Created:** {_format_display_time(created_at, timezone_name)}")
    exploration = getattr(file_, "exploration_summary", None)
    if exploration:
        lines.append("")
        lines.append("## Exploration Summary")
        lines.append("")
        lines.append(exploration)
    else:
        lines.append("")
        lines.append("*No exploration summary available.*")

    trimmed_text, truncated = _truncate_lines_to_cap(
        lines,
        reason_hint="the file's exploration summary is large; trim externally if needed",
    )
    return tool_result(
        {
            "type": "file",
            "text": trimmed_text,
            "truncated": truncated,
        },
    )


# ===========================================================================
# SQL helpers (used by the expansion paths)
# ===========================================================================
#
# These bypass the SummaryStore because they're the tight path the TS source
# inlines (one COUNT, one IN-IN-IN SELECT, one JOIN+LIMIT/OFFSET). The
# store-level API would multiply queries unnecessarily.


def _count_raw_children(parent_summary_id: str, *, conn: sqlite3.Connection) -> int:
    """Return the **raw** (suppression-blind) child count for ``parent_summary_id``.

    Mirrors TS lines 325-332 + 437-445. The store-level
    :meth:`SummaryStore.get_summary_children` defaults to
    ``include_suppressed=False``, so ``len(child_ids)`` only reflects
    visible children. To distinguish "no children" from "all suppressed"
    we re-query the raw count via the underlying
    ``summary_parents`` table.

    Args:
        parent_summary_id: The parent summary's ID.
        conn: An open :class:`sqlite3.Connection`.

    Returns:
        Non-negative integer raw count. Falls back to 0 on any SQL error.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM summary_parents WHERE parent_summary_id = ?",
            (parent_summary_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    if row is None:
        return 0
    # sqlite3 row tuple or dict-like
    n = row[0] if not isinstance(row, sqlite3.Row) else row["n"]
    try:
        return max(0, int(n or 0))
    except (TypeError, ValueError):
        return 0


def _fetch_children_rows(
    child_summary_ids: list[str],
    *,
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Fetch summaries by IDs (suppression-filtered, ordered by created_at).

    Mirrors TS lines 466-481. The IN-list query is a one-shot read of
    ``(summary_id, kind, content, token_count, created_at)`` for the
    requested IDs, filtered to ``suppressed_at IS NULL`` and ordered by
    ``created_at ASC`` for stable output.
    """
    if not child_summary_ids:
        return []
    placeholders = ",".join("?" for _ in child_summary_ids)
    sql = (
        f"SELECT summary_id, kind, content, token_count, created_at "
        f"FROM summaries "
        f"WHERE summary_id IN ({placeholders}) "
        f"AND suppressed_at IS NULL "
        f"ORDER BY created_at ASC"
    )
    cur = conn.execute(sql, child_summary_ids)
    rows: list[dict[str, Any]] = []
    columns = [d[0] for d in cur.description]
    for raw_row in cur.fetchall():
        rows.append(dict(zip(columns, raw_row, strict=False)))
    return rows


def _count_source_messages(summary_id: str, *, conn: sqlite3.Connection) -> int:
    """Return total visible source-message count for a leaf summary.

    Mirrors TS lines 570-579. JOIN ``summary_messages`` × ``messages``
    filtered to ``m.suppressed_at IS NULL``. Used to drive the
    capped-vs-ok status + pagination hint.
    """
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM summary_messages sm
            JOIN messages m ON m.message_id = sm.message_id
            WHERE sm.summary_id = ?
              AND m.suppressed_at IS NULL
            """,
            (summary_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    if row is None:
        return 0
    n = row[0] if not isinstance(row, sqlite3.Row) else row["n"]
    try:
        return max(0, int(n or 0))
    except (TypeError, ValueError):
        return 0


def _fetch_source_messages(
    summary_id: str,
    *,
    limit: int,
    offset: int,
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Fetch source messages for a leaf summary with pagination.

    Mirrors TS lines 581-597. Suppression-filtered and ordered by
    ``m.created_at ASC``.
    """
    cur = conn.execute(
        """
        SELECT m.message_id, m.role, m.content, m.token_count, m.created_at
        FROM summary_messages sm
        JOIN messages m ON m.message_id = sm.message_id
        WHERE sm.summary_id = ?
          AND m.suppressed_at IS NULL
        ORDER BY m.created_at ASC
        LIMIT ? OFFSET ?
        """,
        (summary_id, limit, offset),
    )
    rows: list[dict[str, Any]] = []
    columns = [d[0] for d in cur.description]
    for raw_row in cur.fetchall():
        rows.append(dict(zip(columns, raw_row, strict=False)))
    return rows


# ===========================================================================
# Misc helpers
# ===========================================================================


def _normalize_requested_token_cap(value: Any) -> Optional[int]:
    """Coerce a request ``tokenCap`` param to a positive integer or None.

    Mirrors TS ``normalizeRequestedTokenCap`` (lines 118-123): accepts
    only finite numbers, floors at 1, truncates fractional parts.
    """
    if isinstance(value, bool):
        return None  # bool subclasses int — reject explicitly
    if not isinstance(value, (int, float)):
        return None
    # Reject NaN / +-inf
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return max(1, int(value))


def _clamp_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Clamp ``value`` to ``[minimum, maximum]`` with fallback to ``default``.

    Used for the expansion-limit / offset clamps. Rejects bools (an
    ``int`` subclass) and non-finite floats — those use the default.
    """
    # bool is an int subclass — reject first.
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return default
    try:
        raw_int = int(value) if isinstance(value, (int, float)) else default
    except (TypeError, ValueError):
        return default
    if raw_int < minimum:
        return minimum
    if raw_int > maximum:
        return maximum
    return raw_int


def _format_display_time(value: Any, timezone_name: str) -> str:
    """Format a timestamp for display in describe output. Mirrors TS lines 47-59.

    Accepts :class:`datetime`, string, number (epoch), ``None``, or
    invalid input. Returns ``"-"`` for missing / unparseable input,
    otherwise a ``YYYY-MM-DD HH:MM TZ`` string per the LCM
    :func:`_format_timestamp` convention.
    """
    if value is None:
        return "-"
    dt: Optional[datetime] = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return "-"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return "-"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return "-"
    else:
        return "-"

    if dt is None:
        return "-"
    # Lazy import to dodge a top-level cycle with compaction.py (which
    # itself imports from store/conversation, which transitively imports
    # tools-package). Pulling the helper at call time keeps the import
    # graph free of cycles.
    from lossless_hermes.compaction import _format_timestamp  # noqa: PLC0415

    try:
        return _format_timestamp(dt, timezone_name)
    except (TypeError, ValueError):
        return "-"


def _truncate_lines_to_cap(
    lines: list[str],
    *,
    reason_hint: str,
) -> tuple[str, bool]:
    """Join ``lines`` with newlines, capping cumulative chars at MAX_RESULT_CHARS.

    Mirrors TS ``truncateLinesToCap`` (lines 27-45). When the cumulative
    char count would exceed :data:`MAX_RESULT_CHARS`, the function
    stops, appends a blank line + :func:`truncation_notice`, and
    returns ``(text, True)``. Otherwise the full ``"\\n".join(lines)``
    is returned with ``(text, False)``.

    Per **Wave-12 W1A8 #3** (TS lines 15-26): describe was previously
    unbounded — a single ``describe(condensed_id, expandChildren=true)``
    against a wide condensed could emit ~210K tokens. This char cap
    mirrors lcm_grep's truncation policy as a secondary cap behind the
    main runWithTokenGate refusal.

    Args:
        lines: The accumulated output lines (no trailing newlines).
        reason_hint: Tool-specific reason phrase passed through to
            :func:`truncation_notice`.

    Returns:
        ``(joined_text, truncated_flag)`` tuple. ``joined_text`` is
        the rendered output (with truncation notice appended if cut).
        ``truncated_flag`` is True when at least one line was dropped.
    """
    total = 0
    out: list[str] = []
    for line in lines:
        # +1 for the newline that join("\n") will insert between lines.
        next_total = total + len(line) + (1 if out else 0)
        if next_total > MAX_RESULT_CHARS:
            out.append("")
            out.append(truncation_notice(reason_hint))
            return ("\n".join(out), True)
        out.append(line)
        total = next_total
    return ("\n".join(out), False)
