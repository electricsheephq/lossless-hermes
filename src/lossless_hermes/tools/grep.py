"""Port of ``lcm_grep`` — multi-mode search over compacted conversation history.

Ports ``lossless-claw/src/tools/lcm-grep-tool.ts`` (LCM commit ``1f07fbd`` on
branch ``pr-613``, 1179 LOC TS → Wave A subset: ~600 LOC Python). The
TypeBox-declared schema lives at TS lines 43-125; the handler body at
lines 191-440 (the ``execute`` closure) plus the ``runVerbatimLcmGrep``
helper at lines 947-1161. Both are translated structurally verbatim per
ADR-016 (description prose byte-identical from TS source).

What this tool does
-------------------

``lcm_grep`` is the **most-used** retrieval tool. Five search modes share a
single schema:

1. ``regex`` — Python ``re.search`` against summaries.content and/or
   messages.content via the store-layer ``search_summaries`` /
   ``search_messages`` methods. Pure SQLite.
2. ``full_text`` — FTS5 ``MATCH`` against ``summaries_fts`` /
   ``messages_fts``. The store-layer :func:`sanitize_fts5_query` already
   wraps problematic chars in phrase quotes — DO NOT re-sanitize.
3. ``verbatim`` — return FULL untruncated message rows for citation.
   Hard-capped at 20 results because full message bodies can be large.
   Bypasses the store layer and runs a custom FTS5/LIKE query against
   ``messages`` so the local ``sanitize_fts5_pattern`` rewrites apply
   (TS lines 154-178).
4. ``hybrid`` and 5. ``semantic`` — DEFERRED to issue 06-09 (Wave B).
   The schema advertises the modes so the tool description prose stays
   verbatim from TS; the dispatch returns an ``"<mode> mode is not yet
   available..."`` error.

Wave-12 F5 invariant — middleware-not-decorator
-----------------------------------------------

Per [ADR-029](../../docs/adr/029-wave-fix-provenance.md) Wave-12 F5,
:func:`handle_lcm_grep` is the **inner** handler — it must be wrapped by
``run_with_token_gate`` middleware at the **dispatch layer**
(``LCMEngine.handle_tool_call`` per issue 06-02). The TS source uses
``runWithTokenGate({...inner: async () => {...}})`` at lines 216-437 to
funnel every return through a single tap exit, structurally eliminating
the F5 antipattern. The Python port reproduces this invariant by keeping
the handler body free of token-gate calls.

Token-gate estimator (per [issue 06-03](06-03-runwithtokengate-middleware.md))
------------------------------------------------------------------------------

* ``regex`` / ``full_text``: ``200 + limit * 200`` chars.
* ``verbatim``: ``70 + min(20, limit) * 2400`` chars (large because full
  message rows).

These are caller-provided to the gate; the handler itself does NOT call
the gate.

Architecture seams
------------------

The handler does NOT depend on ``LCMEngine`` directly — instead it takes
a narrow ``GrepContext`` Protocol that exposes:

* ``conn: sqlite3.Connection`` — for the raw verbatim-mode query against
  ``messages`` (bypasses the store layer for the local sanitizer + 20-cap).
* ``summary_store: SummaryStore`` — for regex / full_text searches over
  ``summaries`` (and CJK trigram fallback).
* ``conversation_store: ConversationStore`` — for regex / full_text
  searches over ``messages`` AND for the conversation-scope resolver.
* ``timezone: str`` — passed through to the timestamp formatter.

References
----------

* TS source: ``lossless-claw/src/tools/lcm-grep-tool.ts`` (1179 LOC).
* Porting guide: ``docs/porting-guides/tools.md`` §"lcm_grep".
* Issue spec: ``epics/06-tools/06-08-lcm-grep-regex-fulltext.md``.
* [ADR-016](../../docs/adr/016-typebox-translation.md) — TypeBox
  hand-translate policy (description prose byte-identical).
* [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-12 F5
  (middleware-not-decorator), Wave-12 N3 (truncation regex pin).
* TS test fixture: ``test/lcm-grep-verbatim-mode.test.ts`` (435 LOC).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Optional, Protocol

from lossless_hermes.plugin import result_budget as _result_budget
from lossless_hermes.plugin.result_budget import truncation_notice
from lossless_hermes.store.conversation import (
    ConversationStore,
    MessageSearchInput,
)
from lossless_hermes.store.full_text_fallback import contains_cjk
from lossless_hermes.store.summary import (
    SummaryStore,
    SummarySearchInput,
)
from lossless_hermes.tools import TOOL_SCHEMAS
from lossless_hermes.tools._common import tool_result
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
    parse_iso_timestamp_param,
    resolve_lcm_conversation_scope,
)

__all__ = (
    "LCM_GREP_DESCRIPTION",
    "LCM_GREP_SCHEMA",
    "GrepContext",
    "handle_lcm_grep",
    "sanitize_fts5_pattern",
)


# ===========================================================================
# Schema — verbatim from TS source (ADR-016 §Consequences)
# ===========================================================================
#
# Description prose is byte-identical to lcm-grep-tool.ts lines 196-204
# (the tool-level `description:` block) and the per-field `description`
# strings at lines 43-125. The mechanical TypeBox → dict translation uses
# the helpers in `_typebox.py`.

LCM_GREP_DESCRIPTION: Final[str] = (
    "Search compacted conversation history with FIVE modes (`mode` parameter): "
    "(1) `regex` — literal or regex pattern over summary content; "
    "(2) `full_text` — FTS5 keyword search; queries use FTS5 AND semantics by default, so keep them short and focused; quoted phrases stay intact and optional sort modes can prioritize relevance for older topics; "
    "(3) `hybrid` — FTS5 + Voyage semantic + rerank (PRIMARY for Type B topic-anchored queries: 'have we ever discussed X', 'what work has been done on Y' — handles paraphrases like 'merge mess' → 'rebase blew up'); "
    "(4) `semantic` — pure-vector KNN over summaries via Voyage embed (no rerank, cheaper than hybrid). Use for paraphrastic exploration where keyword precision doesn't matter; "
    "(5) `verbatim` — returns FULL untruncated source messages (PRIMARY for Type C verbatim/citation queries: 'what exactly did X say about Y', 'quote me the original wording'). "
    "Optional `summaryKinds` filter (mode='semantic' / 'hybrid' only) scopes hits to ['leaf'] or ['condensed'] — useful when you want fresh source leaves vs higher-level rollups. "
    "Returns matching snippets with summary/message IDs for follow-up with lcm_describe (one-hop) or lcm_expand_query (multi-hop drilldown). "
    "Tool result is hard-capped at LCM_TOOL_RESULT_TOKEN_BUDGET (default 10K tokens / 40K chars) — when context is near full, prefer narrower queries (smaller `limit`, more specific `pattern`) over big sweeps; chained calls accumulate context, and compaction only fires post-turn."
)
"""Verbatim from ``lcm-grep-tool.ts:196-204``. Per ADR-016 §Consequences
this is the load-bearing model-facing prose that drives tool selection."""


LCM_GREP_SCHEMA: Final[dict[str, Any]] = tool_schema(
    name="lcm_grep",
    description=LCM_GREP_DESCRIPTION,
    parameters=object_schema(
        pattern=string_field(
            'Search pattern. Interpreted as regex when mode is "regex", or as an FTS5 text query when mode is "full_text". In full_text mode, FTS5 defaults to AND matching, so prefer 1-3 distinctive terms or one quoted multi-word phrase instead of padding with synonyms or extra keywords.',
        ),
        mode=optional(
            string_field(
                'Search mode: "regex" for regular expression matching, "full_text" for text search, "hybrid" to blend FTS + semantic vector search via Voyage rerank, "semantic" for pure-vector recall (no FTS, no rerank — cheapest semantic mode), or "verbatim" to return FULL untruncated content of matched messages (for citation / quote-back use cases where the agent needs literal wording). "hybrid" and "semantic" return hits scoped to summaries only (semantic doesn\'t cover raw messages); "verbatim" returns full message rows and is hard-capped at 20 results. Default: "regex".',
                enum=["regex", "full_text", "hybrid", "semantic", "verbatim"],
            ),
        ),
        scope=optional(
            string_field(
                'What to search: "messages" for raw messages, "summaries" for compacted summaries, "both" for all. Default: "both".',
                enum=["messages", "summaries", "both"],
            ),
        ),
        conversationId=optional(
            number_field(
                "Physical conversation ID to search within. If omitted, defaults to the current session family.",
            ),
        ),
        allConversations=optional(
            boolean_field(
                "Set true to explicitly search across all conversations. Ignored when conversationId is provided.",
            ),
        ),
        since=optional(
            string_field(
                "Only return matches created at or after this ISO timestamp.",
            ),
        ),
        before=optional(
            string_field(
                "Only return matches created before this ISO timestamp.",
            ),
        ),
        limit=optional(
            number_field(
                "Maximum number of results to return (default: 50).",
                minimum=1,
                maximum=200,
            ),
        ),
        sort=optional(
            string_field(
                'Sort order: "recency" (newest first, default), "relevance" (best FTS5 match first, full_text mode only), or "hybrid" (full_text mode only; balances relevance with recency). Applied before limit is enforced.',
                enum=["recency", "relevance", "hybrid"],
            ),
        ),
        role=optional(
            string_field(
                'Restrict matches to messages of this role. Useful in verbatim mode where tool-role messages (code grep output, audit blobs) often crowd out user/assistant turns. Accepts "user", "assistant", "tool", "system", or "all" (default). Honored only by mode="verbatim" — other modes already match summaries that have no role.',
                enum=["user", "assistant", "tool", "system", "all"],
            ),
        ),
        summaryKinds=optional(
            array_field(
                string_field(enum=["leaf", "condensed"]),
                description=(
                    "Filter by summary kind. Defaults to both 'leaf' and 'condensed'. "
                    "Honored only by mode='semantic' and 'hybrid'. Useful when the agent "
                    "wants to scope to high-level rollups (kind='condensed') or fresh "
                    "leaves (kind='leaf') instead of both."
                ),
            ),
        ),
    ),
)
"""OpenAI-function-call schema for ``lcm_grep``. Verbatim translation of
the TypeBox declaration at ``lcm-grep-tool.ts:43-125`` per ADR-016."""


# Register at module import time per the TOOL_SCHEMAS contract documented
# in tools/__init__.py. The 06-02 dispatch table reads via
# ``get_tool_schemas()`` so this side-effect is what makes the tool
# discoverable to the LCMEngine.
TOOL_SCHEMAS.append(LCM_GREP_SCHEMA)


# ===========================================================================
# Constants and limits
# ===========================================================================

_DEFAULT_LIMIT: Final[int] = 50
"""Default ``limit`` when the caller omits it (TS line 244)."""

_VERBATIM_HARD_CAP: Final[int] = 20
"""Hard cap on verbatim-mode rows regardless of caller's ``limit``
(TS line 243). Full message rows can be large; this is the load-bearing
protection against blowing past :data:`MAX_RESULT_CHARS`."""

_SNIPPET_MAX_LEN: Final[int] = 200
"""Truncation length for inline snippets in regex/full_text output
(TS line 127)."""

_PER_HIT_CONTENT_CHAR_CAP: Final[int] = 5_000
"""Per-hit verbatim ``content`` cap inside ``details.hits[]``
(TS line 1113). Wave-12 reviewer F6 — full body via lcm_describe."""

_VALID_ROLES: Final[frozenset[str]] = frozenset(
    {"user", "assistant", "tool", "system"},
)
"""Roles accepted by the SQL ``m.role = ?`` filter in verbatim mode
(TS line 989). ``"all"`` is the "no filter" sentinel and is dropped before
the SQL bind."""


# ===========================================================================
# sanitize_fts5_pattern — local verbatim-mode FTS5 pattern wrapper
# ===========================================================================
#
# Port of TS sanitizeFts5Pattern (lines 154-178). DISTINCT from the
# store-layer ``sanitize_fts5_query`` (which wraps EVERY token). This
# one only wraps when the pattern contains chars FTS5's default
# tokenizer treats as separators / operators — leaving already-quoted
# phrases and FTS5-boolean expressions alone.

_PHRASE_QUOTED_RE: Final[re.Pattern[str]] = re.compile(r'^".*"$', re.DOTALL)
"""Match a pattern that is fully wrapped in double quotes."""

_FTS5_OPERATOR_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:AND|OR|NOT|NEAR)\b",
)
"""Match FTS5 boolean operators (case-sensitive — TS uses literal upper)."""

_PROBLEM_CHAR_RE: Final[re.Pattern[str]] = re.compile(r"[.\[\]+*^:/\\!~]")
"""Match chars FTS5's default tokenizer treats as separators/operators
when present BARE (not inside a phrase). TS line 169."""

_STARTS_OR_ENDS_WITH_HYPHEN_RE: Final[re.Pattern[str]] = re.compile(r"^-|-$")
"""Match patterns that start OR end with a hyphen (TS line 170)."""


def sanitize_fts5_pattern(pattern: str) -> str:
    """Wrap problematic FTS5 patterns in phrase quotes (TS lines 154-178).

    P7 fix (2026-05-06 harness): FTS5 ``MATCH`` chokes on bare non-tokenizer
    characters in user input (``v4.1``, ``[brackets]``, hyphenated terms,
    leading/trailing operators). Users hit opaque ``"fts5: syntax error"``
    with no recovery hint.

    Strategy: detect patterns that FTS5 would reject AS-IS, and auto-wrap
    them in double quotes (FTS5 phrase syntax — literal multi-token match).
    Leave already-quoted patterns alone (user explicitly opted-in to FTS5
    phrase semantics) AND leave patterns containing FTS5 boolean operators
    alone (``AND``, ``OR``, ``NEAR(...)``).

    For verbatim mode this is always-on because verbatim is by definition
    "I want literal text." For full_text mode the store-layer
    :func:`sanitize_fts5_query` already handles per-token wrapping, so we
    DO NOT call this from the full_text dispatch.

    Args:
        pattern: The raw user input.

    Returns:
        The original pattern, or a phrase-quoted version of it. Internal
        double quotes are FTS5-escaped (doubled) per TS line 174.

    Examples:
        >>> sanitize_fts5_pattern("v4.1")
        '"v4.1"'
        >>> sanitize_fts5_pattern("hello world")
        'hello world'
        >>> sanitize_fts5_pattern('"already quoted"')
        '"already quoted"'
        >>> sanitize_fts5_pattern("foo AND bar")
        'foo AND bar'
        >>> sanitize_fts5_pattern("-leading-hyphen")
        '"-leading-hyphen"'
    """
    trimmed = pattern.strip()
    if not trimmed:
        return trimmed
    # Already double-quoted phrase — user knows what they're doing.
    if len(trimmed) >= 2 and _PHRASE_QUOTED_RE.match(trimmed):
        return trimmed
    # Contains FTS5 boolean operators or grouping — assume user knows FTS5.
    if _FTS5_OPERATOR_RE.search(trimmed) or "(" in trimmed or ")" in trimmed:
        return trimmed
    if _PROBLEM_CHAR_RE.search(trimmed) or _STARTS_OR_ENDS_WITH_HYPHEN_RE.search(trimmed):
        # Wrap as a phrase. Escape internal double quotes by doubling them
        # (FTS5's escape convention — TS line 174).
        escaped = trimmed.replace('"', '""')
        return f'"{escaped}"'
    return trimmed


# ===========================================================================
# GrepContext — narrow Protocol exposing what the handler needs
# ===========================================================================


class GrepContext(Protocol):
    """The handler's collaborator surface.

    Mirrors the slice of :class:`~lossless_hermes.engine.LCMEngine` that
    ``lcm_grep`` actually needs. Using a structural Protocol keeps the
    handler decoupled from the engine class shape and lets tests
    construct a tiny stand-in dataclass.

    Required attributes:

    * ``conn``: :class:`sqlite3.Connection` for the raw verbatim-mode
      query (FTS5 + LIKE fallback over ``messages``).
    * ``summary_store``: :class:`SummaryStore` for regex/full_text
      searches over ``summaries``.
    * ``conversation_store``: :class:`ConversationStore` for regex /
      full_text searches over ``messages`` AND the conversation-scope
      resolver.
    * ``timezone``: IANA timezone name for the timestamp formatter
      (e.g. ``"UTC"``, ``"America/Los_Angeles"``).
    """

    conn: sqlite3.Connection
    summary_store: SummaryStore
    conversation_store: ConversationStore
    timezone: str


@dataclass
class _LcmScopeAdapter:
    """Adapter that satisfies the ``_LcmLike`` protocol in conversation_scope.

    The conversation-scope resolver consumes anything with a
    ``_conversation_store`` attribute. The :class:`GrepContext` exposes
    ``conversation_store`` (no leading underscore), so we adapt at the
    call site.
    """

    _conversation_store: Optional[ConversationStore]


# ===========================================================================
# Handler entry point
# ===========================================================================


def handle_lcm_grep(
    args: dict[str, Any],
    *,
    ctx: GrepContext,
    deps: LcmDependencies,
    session_key: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Handle an ``lcm_grep`` tool call.

    **Wave-12 F5 invariant:** this is the INNER handler. The
    ``run_with_token_gate`` middleware MUST wrap this call at the
    dispatch layer (issue 06-02 — ``LCMEngine.handle_tool_call``); see
    the module docstring's "Wave-12 F5" section.

    Args:
        args: The tool-call ``arguments`` dict from the LLM provider.
            Read defensively — see :mod:`lossless_hermes.tools._common`.
        ctx: A :class:`GrepContext` exposing the SQL / store / timezone
            collaborator surface.
        deps: :class:`LcmDependencies` slice (the same dataclass
            ``resolve_lcm_conversation_scope`` consumes).
        session_key: Optional cross-conversation session-family key. If
            omitted, the handler falls through to ``session_id`` for
            scope resolution.
        session_id: Optional runtime session id. Either ``session_key``
            or ``session_id`` should be supplied so the scope resolver
            can find an anchor conversation.

    Returns:
        A JSON string per the :func:`tool_result` contract — Hermes's
        :py:meth:`ContextEngine.handle_tool_call` consumes JSON strings,
        not structured dicts. The wrap layer at 06-02 may re-encode for
        the eventual ``{content, details}`` shape, but the handler itself
        returns JSON.

    Tool-error payloads (returned as JSON strings):

    * Empty pattern: ``{"error": "`pattern` is required..."}`` (TS line 234).
    * Invalid timestamp: ``{"error": "<key> must be a valid ISO timestamp."}``.
    * ``since >= before``: ``{"error": "`since` must be earlier than `before`."}``.
    * No conversation scope: ``{"error": "No LCM conversation found..."}``.
    * ``mode='hybrid'`` / ``mode='semantic'``: ``{"error": "<mode> mode is not yet available..."}`` (deferred to 06-09).

    Success payloads are :func:`tool_result`-encoded dicts with the
    rendered text plus a ``details`` slice (mode + counts + truncation
    flag + role/sort overrides).
    """
    # ----- Param read --------------------------------------------------------
    # Pattern is required and MUST be a non-empty string. The TS source
    # reads ``p.pattern as string`` (TS line 230) which would NPE on
    # ``undefined``; we accept ``str`` only and reject everything else
    # via the empty-pattern guard below.
    raw_pattern = args.get("pattern")
    pattern = raw_pattern.strip() if isinstance(raw_pattern, str) else ""
    # Wave-1 Auditor #9 + QA-runner adv-empty-pattern fix (TS lines 232-238):
    # empty pattern was reaching FTS5 sanitizer which returns `'""'`, causing
    # FTS5 to match all rows. Reject explicitly.
    if not pattern:
        return tool_result(
            {"error": "`pattern` is required and must be a non-empty string."},
        )

    mode_raw = args.get("mode")
    mode = mode_raw if isinstance(mode_raw, str) else "regex"
    if mode not in ("regex", "full_text", "hybrid", "semantic", "verbatim"):
        # Unknown mode — TS would default to "regex" implicitly (no validation
        # in the JS layer); we match that behavior.
        mode = "regex"

    # ----- Wave A: defer hybrid + semantic to issue 06-09 --------------------
    if mode == "hybrid":
        return tool_result(
            {
                "error": (
                    "hybrid mode is not yet available in this build. "
                    "Use mode='full_text' for keyword search."
                ),
            },
        )
    if mode == "semantic":
        return tool_result(
            {
                "error": (
                    "semantic mode is not yet available in this build. "
                    "Use mode='full_text' for keyword search."
                ),
            },
        )

    scope_raw = args.get("scope")
    scope = scope_raw if isinstance(scope_raw, str) else "both"
    if scope not in ("messages", "summaries", "both"):
        scope = "both"

    # TS line 244-246: requestedLimit defaults to 50; in verbatim mode it
    # is hard-capped to 20.
    requested_limit_raw = args.get("limit")
    if isinstance(requested_limit_raw, bool):
        requested_limit = _DEFAULT_LIMIT
    elif isinstance(requested_limit_raw, (int, float)):
        try:
            requested_limit = int(requested_limit_raw)
        except (ValueError, OverflowError):
            requested_limit = _DEFAULT_LIMIT
        if requested_limit < 1:
            requested_limit = 1
        if requested_limit > 200:
            requested_limit = 200
    else:
        requested_limit = _DEFAULT_LIMIT
    limit = min(requested_limit, _VERBATIM_HARD_CAP) if mode == "verbatim" else requested_limit

    # TS line 247-253: sort defaults to "recency"; silently overridden to
    # "recency" for non-full_text modes. Wave-7 Auditor #8 P1: surface
    # `sortIgnored` field when caller explicitly passed a non-recency sort
    # with a mode that doesn't support it.
    requested_sort_raw = args.get("sort")
    requested_sort = requested_sort_raw if isinstance(requested_sort_raw, str) else "recency"
    if requested_sort not in ("recency", "relevance", "hybrid"):
        requested_sort = "recency"
    effective_sort = requested_sort if mode == "full_text" else "recency"
    sort_ignored = (
        args.get("sort") is not None and requested_sort != "recency" and mode != "full_text"
    )

    # ----- Timestamp filters -------------------------------------------------
    try:
        since = parse_iso_timestamp_param(args, "since")
        before = parse_iso_timestamp_param(args, "before")
    except ValueError as exc:
        return tool_result({"error": str(exc)})

    if since is not None and before is not None and since >= before:
        return tool_result({"error": "`since` must be earlier than `before`."})

    # ----- Conversation scope ------------------------------------------------
    conversation_scope = resolve_lcm_conversation_scope(
        lcm=_LcmScopeAdapter(_conversation_store=ctx.conversation_store),
        params=args,
        session_id=session_id,
        session_key=session_key,
        deps=deps,
    )
    if not conversation_scope.all_conversations and conversation_scope.conversation_id is None:
        return tool_result(
            {
                "error": (
                    "No LCM conversation found for this session. "
                    "Provide conversationId or set allConversations=true."
                ),
            },
        )

    # ----- Verbatim mode ----------------------------------------------------
    if mode == "verbatim":
        role_raw = args.get("role")
        role_filter_raw = role_raw.strip() if isinstance(role_raw, str) else ""
        role_filter = role_filter_raw if role_filter_raw and role_filter_raw != "all" else None
        return _run_verbatim_lcm_grep(
            ctx=ctx,
            pattern=pattern,
            conversation_scope=conversation_scope,
            since=since,
            before=before,
            limit=limit,
            role_filter=role_filter,
        )

    # ----- Regex / full_text mode ------------------------------------------
    return _run_regex_or_full_text_grep(
        ctx=ctx,
        pattern=pattern,
        mode=mode,
        scope=scope,
        conversation_scope=conversation_scope,
        since=since,
        before=before,
        limit=limit,
        effective_sort=effective_sort,
        requested_sort=requested_sort,
        sort_ignored=sort_ignored,
    )


# ===========================================================================
# Regex / full_text path (TS lines 341-435)
# ===========================================================================


def _truncate_snippet(content: str, max_len: int = _SNIPPET_MAX_LEN) -> str:
    """Truncate a multi-line content blob to a single-line snippet.

    Mirrors TS ``truncateSnippet`` (lines 127-133): collapses newlines to
    spaces, trims, and clips at ``max_len`` with a ``"..."`` suffix.
    """
    single_line = content.replace("\n", " ").strip()
    if len(single_line) <= max_len:
        return single_line
    return single_line[: max_len - 3] + "..."


def _run_regex_or_full_text_grep(
    *,
    ctx: GrepContext,
    pattern: str,
    mode: str,
    scope: str,
    conversation_scope: Any,
    since: Optional[datetime],
    before: Optional[datetime],
    limit: int,
    effective_sort: str,
    requested_sort: str,
    sort_ignored: bool,
) -> str:
    """Run a regex or full_text search over messages and/or summaries.

    Mirrors TS lines 341-435 — the non-hybrid/semantic/verbatim branch.
    Routes to ``ConversationStore.search_messages`` and/or
    ``SummaryStore.search_summaries`` based on ``scope``. The store layer
    handles FTS5 sanitization, CJK fallback, and LIKE fallback.
    """
    messages: list[Any] = []
    summaries: list[Any] = []

    # Conversation scoping — both stores accept a single id or a list.
    conversation_id = (
        None if conversation_scope.all_conversations else conversation_scope.conversation_id
    )
    conversation_ids = (
        None if conversation_scope.all_conversations else conversation_scope.conversation_ids
    )

    if scope in ("messages", "both"):
        # ``MessageSearchInput`` accepts only "regex" or "full_text" modes
        # (per the type alias). We've already filtered hybrid/semantic
        # above and verbatim has its own path.
        msg_input = MessageSearchInput(
            query=pattern,
            mode=mode,  # type: ignore[arg-type]
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
            since=since,
            before=before,
            limit=limit,
            sort=effective_sort,  # type: ignore[arg-type]
        )
        messages = list(ctx.conversation_store.search_messages(msg_input))

    if scope in ("summaries", "both"):
        sum_input = SummarySearchInput(
            query=pattern,
            mode=mode,  # type: ignore[arg-type]
            conversation_id=conversation_id,
            conversation_ids=conversation_ids,
            since=since,
            before=before,
            limit=limit,
            sort=effective_sort,  # type: ignore[arg-type]
        )
        summaries = list(ctx.summary_store.search_summaries(sum_input))

    total_matches = len(messages) + len(summaries)

    # ----- Render markdown ---------------------------------------------------
    lines: list[str] = []
    lines.append("## LCM Grep Results")
    lines.append(f"**Pattern:** `{pattern}`")
    lines.append(
        f"**Mode:** {mode} | **Scope:** {scope} | **Sort:** {effective_sort}",
    )
    if conversation_scope.all_conversations:
        lines.append("**Conversation scope:** all conversations")
    elif conversation_scope.conversation_id is not None:
        family_count = (
            len(conversation_scope.conversation_ids) if conversation_scope.conversation_ids else 0
        )
        if family_count > 1:
            lines.append(
                f"**Conversation scope:** session family rooted at "
                f"{conversation_scope.conversation_id} "
                f"({family_count} segments)",
            )
        else:
            lines.append(
                f"**Conversation scope:** {conversation_scope.conversation_id}",
            )
    if since is not None or before is not None:
        since_str = (
            f"since {_format_display_time(since, ctx.timezone)}"
            if since is not None
            else "since -∞"
        )
        before_str = (
            f"before {_format_display_time(before, ctx.timezone)}"
            if before is not None
            else "before +∞"
        )
        lines.append(f"**Time filter:** {since_str} | {before_str}")
    lines.append(f"**Total matches:** {total_matches}")
    lines.append("")

    current_chars = sum(len(line) for line in lines) + len(lines) - 1
    truncated = False
    max_chars = _result_budget.MAX_RESULT_CHARS
    reason_hint = "narrow query, lower limit, or wait for next-turn compaction"

    if messages:
        lines.append("### Messages")
        lines.append("")
        current_chars += len("### Messages") + 1 + 0 + 1
        for msg in messages:
            snippet = _truncate_snippet(msg.snippet)
            time_str = _format_display_time(msg.created_at, ctx.timezone)
            line = f"- [msg#{msg.message_id}] ({msg.role}, {time_str}): {snippet}"
            if current_chars + len(line) > max_chars:
                lines.append(truncation_notice(reason_hint))
                truncated = True
                break
            lines.append(line)
            current_chars += len(line) + 1
        lines.append("")

    if summaries and not truncated:
        lines.append("### Summaries")
        lines.append("")
        current_chars += len("### Summaries") + 1 + 0 + 1
        for sum_ in summaries:
            snippet = _truncate_snippet(sum_.snippet)
            time_str = _format_display_time(sum_.created_at, ctx.timezone)
            line = f"- [{sum_.summary_id}] ({sum_.kind}, {time_str}): {snippet}"
            if current_chars + len(line) > max_chars:
                lines.append(truncation_notice(reason_hint))
                truncated = True
                break
            lines.append(line)
            current_chars += len(line) + 1
        lines.append("")

    if total_matches == 0:
        lines.append("No matches found.")

    text = "\n".join(lines)
    details: dict[str, Any] = {
        "messageCount": len(messages),
        "summaryCount": len(summaries),
        "totalMatches": total_matches,
        # Wave-12 retro N2: top-level `truncated` is the canonical
        # agent-facing contract field across all content-emitting tools.
        "truncated": truncated,
    }
    if sort_ignored:
        # Wave-7 Auditor #8 P1: surface sort override.
        details["sortIgnored"] = True
        details["requestedSort"] = requested_sort
        details["effectiveSort"] = effective_sort

    return tool_result({"text": text, "details": details})


# ===========================================================================
# Verbatim path (TS lines 947-1161)
# ===========================================================================


def _run_verbatim_lcm_grep(
    *,
    ctx: GrepContext,
    pattern: str,
    conversation_scope: Any,
    since: Optional[datetime],
    before: Optional[datetime],
    limit: int,
    role_filter: Optional[str],
) -> str:
    """Run a verbatim-mode search over raw messages.

    Mirrors TS ``runVerbatimLcmGrep`` (lines 947-1161). Returns FULL
    untruncated message content for matches — for citation, quote-back,
    and "show me what was actually said" use cases where the literal
    wording matters and snippets aren't enough.

    Implementation: FTS5 over messages + return full ``m.content`` (not
    snippet). Hard-capped at 20 results because full message rows can
    be large. Filters ``suppressed_at IS NULL`` per §10 invariant. Scope
    is messages only.
    """
    db = ctx.conn

    # Build the SQL query. Mirror conversation-store search_full_text shape
    # but return full m.content instead of snippet.
    filters: list[str] = ["m.suppressed_at IS NULL"]
    binds: list[Any] = []

    # Wave-9 Agent #4 P1 fix (TS lines 960-967): detect CJK queries and
    # route directly through LIKE substring match. messages_fts is
    # created with `tokenize='porter unicode61'` which can't segment CJK
    # ideographs — `messages_fts MATCH '<chinese characters>'` returns 0
    # rows WITHOUT throwing, so the existing exception-driven LIKE
    # fallback never triggers. There is no messages_fts_cjk trigram table
    # for messages (only for summaries). For Chinese/Japanese/Korean
    # conversations every Question-C verbatim query was returning "No
    # verbatim matches" silently. By detecting CJK at the Python layer
    # and skipping FTS entirely we get correct LIKE-based verbatim
    # recall on CJK content.
    use_like_for_cjk = contains_cjk(pattern)

    # Wave-8 P1 fix (TS lines 974-984): track fts_bind_index AT THE PUSH
    # SITE so future refactors that move the FTS bind don't break the
    # LIKE-fallback substitution. Previously hard-coded to 0 with a
    # comment that's brittle to refactor.
    fts_bind_index = len(binds)
    if use_like_for_cjk:
        filters.append("m.content LIKE ?")
        binds.append(f"%{pattern}%")
    else:
        filters.append("messages_fts MATCH ?")
        binds.append(sanitize_fts5_pattern(pattern))

    # P6 fix (TS lines 986-993): role filter — at SQL layer so it composes
    # with FTS5 and doesn't burn the 20-result cap on tool-message blobs
    # when the agent wants user or assistant turns. Audit 2 finding #2:
    # include 'system'.
    if role_filter and role_filter in _VALID_ROLES:
        filters.append("m.role = ?")
        binds.append(role_filter)

    if conversation_scope.all_conversations:
        pass  # no conversation filter
    elif conversation_scope.conversation_ids and len(conversation_scope.conversation_ids) > 0:
        placeholders = ",".join("?" for _ in conversation_scope.conversation_ids)
        filters.append(f"m.conversation_id IN ({placeholders})")
        for cid in conversation_scope.conversation_ids:
            binds.append(cid)
    elif conversation_scope.conversation_id is not None:
        filters.append("m.conversation_id = ?")
        binds.append(conversation_scope.conversation_id)

    if since is not None:
        filters.append("julianday(m.created_at) >= julianday(?)")
        binds.append(since.isoformat())
    if before is not None:
        filters.append("julianday(m.created_at) < julianday(?)")
        binds.append(before.isoformat())

    # Best to detect FTS5 absence and fall back to LIKE on m.content.
    where_clause = " AND ".join(filters)
    rows: list[tuple[Any, ...]] = []
    try:
        # Wave-9 Agent #4 P1 fix: when CJK detected at the Python layer
        # above, skip the messages_fts JOIN entirely — the filter is
        # already a direct `m.content LIKE ?` substring match.
        if use_like_for_cjk:
            sql = (
                "SELECT m.message_id, m.conversation_id, m.role, m.content, "
                "m.token_count, m.created_at "
                "FROM messages m "
                f"WHERE {where_clause} "
                "ORDER BY datetime(m.created_at) DESC "
                "LIMIT ?"
            )
        else:
            sql = (
                "SELECT m.message_id, m.conversation_id, m.role, m.content, "
                "m.token_count, m.created_at "
                "FROM messages m "
                "JOIN messages_fts ON messages_fts.rowid = m.rowid "
                f"WHERE {where_clause} "
                "ORDER BY datetime(m.created_at) DESC "
                "LIMIT ?"
            )
        cursor = db.execute(sql, [*binds, limit])
        rows = list(cursor.fetchall())
    except sqlite3.DatabaseError:
        # FTS5 not available — fall back to LIKE on m.content.
        # Audit 3 finding #1 (HIGH) (TS lines 1053-1068): the `binds`
        # array was poisoned by the sanitize_fts5_pattern wrapping above
        # (e.g. `"v4.1"` instead of raw `v4.1`). The previous
        # `findIndex(bb => bb === pattern)` returned -1, so no replacement
        # happened and LIKE got the literal phrase-quoted form, matching
        # nothing on old-SQLite (no-FTS5) installations. Fix: replace the
        # FTS5 bind with the raw LIKE pattern. The bind index was tracked
        # explicitly at the push site (fts_bind_index) so this no longer
        # assumes FTS is the first push.
        fallback_filters = [
            "m.content LIKE ?" if f == "messages_fts MATCH ?" else f for f in filters
        ]
        fallback_binds: list[Any] = [
            f"%{pattern}%" if i == fts_bind_index else b for i, b in enumerate(binds)
        ]
        fallback_sql = (
            "SELECT m.message_id, m.conversation_id, m.role, m.content, "
            "m.token_count, m.created_at "
            "FROM messages m "
            f"WHERE {' AND '.join(fallback_filters)} "
            "ORDER BY datetime(m.created_at) DESC "
            "LIMIT ?"
        )
        cursor = db.execute(fallback_sql, [*fallback_binds, limit])
        rows = list(cursor.fetchall())

    # ----- Render markdown ---------------------------------------------------
    lines: list[str] = []
    lines.append("## LCM Grep Results")
    lines.append(f"**Pattern:** `{pattern}`")
    role_suffix = f" (role={role_filter})" if role_filter else ""
    lines.append(
        f"**Mode:** verbatim | **Scope:** messages{role_suffix} | "
        f"**Cap:** {limit} (full message rows; hard limit 20)",
    )
    if conversation_scope.all_conversations:
        lines.append("**Conversation scope:** all conversations")
    elif conversation_scope.conversation_id is not None:
        lines.append(
            f"**Conversation scope:** {conversation_scope.conversation_id}",
        )
    if since is not None or before is not None:
        since_str = (
            f"since {_format_display_time(since, ctx.timezone)}"
            if since is not None
            else "since -∞"
        )
        before_str = (
            f"before {_format_display_time(before, ctx.timezone)}"
            if before is not None
            else "before +∞"
        )
        lines.append(f"**Time filter:** {since_str} | {before_str}")
    lines.append(f"**Total matches:** {len(rows)}")
    lines.append("")

    # Wave-12 reviewer F6 fix (TS lines 1103-1113): track which rows were
    # emitted into markdown and cap each hit's content at
    # PER_HIT_CONTENT_CHAR_CAP. Pre-fix: ``details.hits[].content``
    # returned full untruncated body for every fetched row regardless of
    # markdown truncation — empirical validation showed 200-385K chars/call
    # leaking through details while markdown capped at 25-33K. Now:
    # details.hits is sliced to rendered_row_count, each hit's content
    # capped at 5K chars (~96th percentile of message lengths in observed
    # corpus). Callers needing full body for a specific message follow up
    # with lcm_describe(messageId, expandMessages=true).
    rendered_row_count = 0
    truncated = False
    max_chars = _result_budget.MAX_RESULT_CHARS
    reason_hint = "narrow time range, lower limit, or wait for next-turn compaction"

    if not rows:
        lines.append(
            "_No verbatim matches in raw messages. "
            "Try mode='regex' or mode='full_text' for broader search._",
        )
    else:
        current_chars = sum(len(line) for line in lines) + len(lines) - 1
        for row in rows:
            (
                message_id,
                _conversation_id,
                role,
                content,
                token_count,
                created_at,
            ) = row
            time_str = _format_display_time(created_at, ctx.timezone)
            header = f"### [msg#{message_id}] {role} — {time_str} ({token_count} tokens)"
            block = f"{header}\n\n{content}\n"
            if current_chars + len(block) > max_chars:
                lines.append(truncation_notice(reason_hint))
                truncated = True
                break
            lines.append(block)
            current_chars += len(block) + 1
            rendered_row_count += 1

    text = "\n".join(lines)
    hits: list[dict[str, Any]] = []
    for row in rows[:rendered_row_count]:
        (
            message_id,
            conversation_id,
            role,
            content,
            token_count,
            created_at,
        ) = row
        full_len = len(content) if isinstance(content, str) else 0
        if isinstance(content, str) and full_len > _PER_HIT_CONTENT_CHAR_CAP:
            capped = (
                content[:_PER_HIT_CONTENT_CHAR_CAP] + "…[truncated; full body via lcm_describe]"
            )
            content_truncated = True
        else:
            capped = content
            content_truncated = False
        hits.append(
            {
                "messageId": message_id,
                "conversationId": conversation_id,
                "role": role,
                "content": capped,
                "contentTruncated": content_truncated,
                "fullContentLength": full_len,
                "tokenCount": token_count,
                "createdAt": (
                    created_at.isoformat() if isinstance(created_at, datetime) else created_at
                ),
            },
        )

    details: dict[str, Any] = {
        "mode": "verbatim",
        "pattern": pattern,
        "totalMatches": len(rows),
        "truncated": truncated,
        "hits": hits,
    }
    return tool_result({"text": text, "details": details})


# ===========================================================================
# Time formatting helper
# ===========================================================================


def _format_display_time(value: Any, timezone_name: str) -> str:
    """Format a timestamp for display in grep output. Mirrors TS lines 29-41.

    Accepts :class:`datetime`, string, number (epoch), ``None``, or invalid
    input. Returns ``"-"`` for missing / unparseable input, otherwise a
    ``YYYY-MM-DD HH:MM TZ`` string per the LCM :func:`_format_timestamp`
    convention.
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
            from datetime import timezone as _tz  # noqa: PLC0415

            dt = datetime.fromtimestamp(float(value), tz=_tz.utc)
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
