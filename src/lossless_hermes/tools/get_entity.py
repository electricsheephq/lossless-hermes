"""Port of ``lcm_get_entity`` — agent tool to look up a NAMED entity by canonical name.

Ports ``lossless-claw/src/tools/lcm-get-entity-tool.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 342 LOC TS → ~340 LOC Python). The
TypeBox-declared schema lives at TS lines 39-67; the handler body at
lines 138-340. Both are translated structurally verbatim per ADR-016
(description prose byte-identical from TS source).

What this tool does
-------------------

``lcm_get_entity`` is the **PRIMARY tool for Type D pattern-anchored
entity queries**: the agent or user NAMES a specific entity ("tell me
about Voyage", "history of customer X", "work I've done with PR #613")
and the tool returns:

1. The entity record (canonical_text, entity_type, first/last_seen_at,
   occurrence_count, alternate_surfaces) — **all aggregates recomputed
   from unsuppressed mentions only** (Wave-12 reviewer P1 fix).
2. A bounded list of mentions across summaries with surface_form,
   summary_id, span_start/end, mentioned_at (descending).

Distinct from sibling tools:

* ``lcm_search_entities`` — fuzzy browse over many entities (different
  surface — substring / type filter, not a single-name lookup).
* ``lcm_grep --mode semantic`` — similarity over leaf content; no entity
  needed.
* ``lcm_grep --mode hybrid`` — paraphrastic search across all summary
  content; preferred when the user asks a topic question WITHOUT
  naming an entity.

Suppression contract
--------------------

Per **Wave-12 reviewer P1** (ADR-029): aggregates AND mention list AND
existence are computed from the visible-mentions filter
(``summaries.suppressed_at IS NULL``). If ALL mentions are suppressed,
the entity is INVISIBLE — the response is byte-identical to "no such
entity" so an attacker cannot probe-by-existence.

The producer-side ``occurrence_count`` column on ``lcm_entities`` is
written by the coreference worker (Epic 07) and never decremented on
suppression — the lossless-bedrock half. The agent-surface
rectification lives HERE: this read goes through
:data:`VISIBLE_MENTIONS_CTE` + :func:`entity_agg_cte`
(``include_first_in=True``) shared with ``lcm_search_entities``.

Wave-12 F5 invariant — middleware-not-decorator
-----------------------------------------------

Per [ADR-029](../../docs/adr/029-wave-fix-provenance.md) Wave-12 F5,
:func:`handle_lcm_get_entity` is the **inner** handler — it must be
wrapped by ``run_with_token_gate`` middleware at the **dispatch
layer** (``LCMEngine.handle_tool_call`` per issue 06-02). The token
gate estimator for ``lcm_get_entity`` is already wired (see
``plugin/needs_compact_gate.py`` line 256: ``250 + mentionLimit * 110``
chars).

Architecture seams
------------------

The handler does NOT depend on :class:`LCMEngine` directly — it takes
a narrow :class:`GetEntityContext` Protocol that exposes:

* ``conn: sqlite3.Connection`` — for the two raw SQL queries the
  handler runs directly (entity lookup CTE; mention list).
* ``timezone: str`` — IANA timezone passed to the timestamp formatter.

This lets tests construct a minimal context dict without spinning up
the full :class:`LCMEngine`, and lets the eventual 06-02 dispatch wrap
pass the engine seam in.

References
----------

* TS source: ``lossless-claw/src/tools/lcm-get-entity-tool.ts`` (342 LOC).
* Porting guide: ``docs/porting-guides/tools.md`` §"lcm_get_entity"
  (lines 396-440).
* Issue spec: ``epics/06-tools/06-10-lcm-get-entity.md``.
* [ADR-016](../../docs/adr/016-typebox-translation.md) — TypeBox
  hand-translate policy (description prose byte-identical).
* [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-12 P1
  (suppression-aware aggregates) and Wave-12 F5
  (middleware-not-decorator).
* TS test fixture: ``test/lcm-get-entity-tool.test.ts`` (480 LOC).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Optional, Protocol

from lossless_hermes.tools import TOOL_SCHEMAS
from lossless_hermes.tools._common import (
    read_number_param,
    read_string_param,
    tool_result,
)
from lossless_hermes.tools._typebox import (
    number_field,
    object_schema,
    optional,
    string_field,
    tool_schema,
)
from lossless_hermes.tools.entity_shared import (
    VISIBLE_MENTIONS_CTE,
    entity_agg_cte,
)

__all__ = (
    "DEFAULT_MENTION_LIMIT",
    "GetEntityContext",
    "LCM_GET_ENTITY_DESCRIPTION",
    "LCM_GET_ENTITY_SCHEMA",
    "MAX_MENTION_LIMIT",
    "MIN_MENTION_LIMIT",
    "handle_lcm_get_entity",
)


# ===========================================================================
# Constants — TS lines 35-37
# ===========================================================================

DEFAULT_MENTION_LIMIT: Final[int] = 20
"""Default ``mentionLimit`` when caller omits it (TS line 35)."""

MIN_MENTION_LIMIT: Final[int] = 1
"""Minimum ``mentionLimit`` (TS line 36)."""

MAX_MENTION_LIMIT: Final[int] = 100
"""Maximum ``mentionLimit`` (TS line 37)."""


# ===========================================================================
# Schema — verbatim from TS source (ADR-016 §Consequences)
# ===========================================================================
#
# Description prose is byte-identical to lcm-get-entity-tool.ts lines
# 123-136 (the `description:` block) and the per-field `description`
# strings at lines 39-66. The mechanical TypeBox → dict translation
# uses the helpers in `_typebox.py`.

LCM_GET_ENTITY_DESCRIPTION: Final[str] = (
    "Look up a NAMED entity (person, project, customer, library, identifier — "
    "things automatically extracted by the entity coreference worker) by "
    "canonical name and return its mentions across the session corpus. "
    "PRIMARY tool for Type D pattern-anchored entity queries when the user "
    "NAMES a specific entity: 'tell me about <X>', 'history of customer <Y>', "
    "'work I've done with <library Z>'. "
    "If the user is asking a paraphrastic topic question without naming an "
    "entity ('have we discussed X-shaped problems', 'what work has been done "
    "on rate limiting'), prefer lcm_grep --mode hybrid instead — it handles "
    "paraphrase across the corpus without needing a canonical entity to exist. "
    "For browsing many entities by substring or by entity_type, use "
    "lcm_search_entities. For raw leaf content similarity (no entity "
    "needed), use lcm_grep --mode semantic."
)
"""Verbatim from ``lcm-get-entity-tool.ts:123-136``. Per ADR-016 §Consequences
this is the load-bearing model-facing prose that drives tool selection.
The Type-D routing prose + fallback-to-hybrid hint are load-bearing per
tools.md lines 401-402."""


LCM_GET_ENTITY_SCHEMA: Final[dict[str, Any]] = tool_schema(
    name="lcm_get_entity",
    description=LCM_GET_ENTITY_DESCRIPTION,
    parameters=object_schema(
        name=string_field(
            "Entity name to look up. Matched COLLATE NOCASE against the canonical "
            "form in lcm_entities (e.g. 'Voyage', 'eva', 'PR #613'). Required.",
        ),
        sessionKey=optional(
            string_field(
                "Session key scope. If omitted, defaults to the current session's key.",
            ),
        ),
        entityType=optional(
            string_field(
                "Optional entity_type filter. Common extractor-produced values: 'person_name', "
                "'pr_number', 'agent_id', 'session_key', 'command', 'file_path', 'date'. Useful when "
                "the same name (e.g. 'main') could match multiple entity types. Discover what's in "
                "the catalog first via lcm_search_entities without an entityType filter.",
            ),
        ),
        mentionLimit=optional(
            number_field(
                f"Max mentions to return (default {DEFAULT_MENTION_LIMIT}; "
                f"range {MIN_MENTION_LIMIT}-{MAX_MENTION_LIMIT}).",
                minimum=MIN_MENTION_LIMIT,
                maximum=MAX_MENTION_LIMIT,
            ),
        ),
    ),
)
"""OpenAI-function-call schema for ``lcm_get_entity``. Verbatim translation
of the TypeBox declaration at ``lcm-get-entity-tool.ts:39-67`` per
ADR-016."""


# Register at module import time per the TOOL_SCHEMAS contract documented
# in tools/__init__.py. The 06-02 dispatch table reads via
# ``get_tool_schemas()`` so this side-effect is what makes the tool
# discoverable to the LCMEngine.
TOOL_SCHEMAS.append(LCM_GET_ENTITY_SCHEMA)


# ===========================================================================
# GetEntityContext — narrow Protocol exposing what the handler needs
# ===========================================================================


class GetEntityContext(Protocol):
    """The handler's collaborator surface.

    Mirrors the slice of :class:`~lossless_hermes.engine.LCMEngine` that
    ``lcm_get_entity`` actually needs. Using a structural Protocol keeps
    the handler decoupled from the engine class shape and lets tests
    construct a tiny stand-in dataclass.

    Required attributes:

    * ``conn``: :class:`sqlite3.Connection` for the two SQL queries the
      handler runs directly (entity lookup CTE; mention list).
    * ``timezone``: IANA timezone name for the timestamp formatter
      (e.g. ``"UTC"``, ``"America/Los_Angeles"``).
    """

    conn: sqlite3.Connection
    timezone: str


# ===========================================================================
# Internal row shapes (the SELECT result rows)
# ===========================================================================
#
# Mirrors the TS interfaces at lines 69-90. Plain dataclasses (not
# Protocols) — these are CONSTRUCTED from sqlite3.Row, not implemented
# externally.


@dataclass(frozen=True)
class _EntityRow:
    """Result of the entity-lookup CTE query (TS interface EntityRow lines 69-80)."""

    entity_id: str
    session_key: str
    canonical_text: str
    entity_type: str
    first_seen_at: str
    last_seen_at: str
    first_seen_in_summary_id: Optional[str]
    occurrence_count: int
    alternate_surfaces: Optional[str]
    metadata: Optional[str]


@dataclass(frozen=True)
class _MentionRow:
    """Result of the mention-list query (TS interface MentionRow lines 82-90)."""

    mention_id: str
    entity_id: str
    summary_id: str
    surface_form: str
    span_start: Optional[int]
    span_end: Optional[int]
    mentioned_at: str


# ===========================================================================
# Handler entry point
# ===========================================================================


def handle_lcm_get_entity(
    args: dict[str, Any],
    *,
    ctx: GetEntityContext,
    session_key: Optional[str] = None,
) -> str:
    """Handle an ``lcm_get_entity`` tool call.

    **Wave-12 F5 invariant:** this is the INNER handler. The
    ``run_with_token_gate`` middleware MUST wrap this call at the
    dispatch layer (issue 06-02 — ``LCMEngine.handle_tool_call``); see
    the module docstring's "Wave-12 F5" section. The wrap MUST happen
    at invocation time, NOT at registration time.

    Args:
        args: The tool-call ``arguments`` dict from the LLM provider.
            Read defensively — see :mod:`lossless_hermes.tools._common`.
        ctx: A :class:`GetEntityContext` exposing the SQL / timezone
            collaborator surface.
        session_key: Optional fallback session key when ``args`` omits
            ``sessionKey``. Mirrors TS ``input.sessionKey``.

    Returns:
        A JSON string per the :func:`tool_result` contract — Hermes's
        :py:meth:`ContextEngine.handle_tool_call` consumes JSON
        strings, not structured dicts.

    Payloads (returned as JSON strings):

    * ``name`` missing/empty: ``{"error": "`name` is required."}``.
    * No session_key resolved: ``{"error": "No session_key resolved..."}``.
    * Not found (or all mentions suppressed): ``{"found": False, ...,
      "fallback_suggestions": [<3 concrete suggestions>]}``.
    * Found: ``{"text": "...", "details": {...mentions list...}}``.

    **Existence-probing defense:** the "not found" payload is
    INTENTIONALLY indistinguishable from "all mentions suppressed" —
    same shape, no leakage that an entity exists but is hidden.
    Pinned by tests.
    """
    # ----- 1. Validate name (TS lines 152-156) -----------------------------
    try:
        name = read_string_param(args, "name", required=True)
    except ValueError:
        return tool_result({"error": "`name` is required."})
    assert name is not None  # required=True guarantees non-None

    # ----- 2. Resolve session_key — param wins, else input.sessionKey ------
    # TS lines 158-167: explicit param wins; else current session's key.
    session_key_param = read_string_param(args, "sessionKey")
    if session_key_param:
        effective_session_key = session_key_param
    elif isinstance(session_key, str) and session_key.strip():
        effective_session_key = session_key.strip()
    else:
        return tool_result(
            {
                "error": (
                    "No session_key resolved. Pass `sessionKey` explicitly "
                    "or call from an active LCM session."
                ),
            },
        )

    # ----- 3. Optional entity_type filter (TS lines 169-172) --------------
    # Lowercase per TS .toLowerCase().
    entity_type_raw = read_string_param(args, "entityType")
    entity_type_filter = entity_type_raw.lower() if entity_type_raw else None

    # ----- 4. Mention limit (TS lines 174-178) ----------------------------
    # Clamp to [MIN, MAX] and floor (Math.trunc equivalent). The
    # read_number_param helper clamps for us; we then int() to truncate.
    mention_limit_float = read_number_param(
        args,
        "mentionLimit",
        minimum=MIN_MENTION_LIMIT,
        maximum=MAX_MENTION_LIMIT,
        default=DEFAULT_MENTION_LIMIT,
    )
    # read_number_param returns None only when default is None — we passed
    # a non-None default so the result is always a float here.
    assert mention_limit_float is not None
    mention_limit = int(mention_limit_float)  # truncates (matches Math.trunc)

    # ----- 5. Entity lookup via VISIBLE_MENTIONS_CTE + entity_agg_cte ------
    # LCM Wave-12 P1 (2026-04): aggregates recompute from UNSUPPRESSED
    # mentions only to prevent suppressed-mention data leaking via
    # aggregate columns. The CTE join also implicitly enforces the
    # EXISTS guard — if no unsuppressed mention, no row in entity_agg
    # → no row returned. "Not found" branch covers BOTH "no such
    # entity" AND "all mentions suppressed" — indistinguishable to the
    # agent by design (operator suppression is the contract).
    # Original: lossless-claw/src/tools/lcm-get-entity-tool.ts (uses lcm-entity-shared.ts CTE).
    entity_filters = [
        "e.session_key = ?",
        "e.canonical_text = ? COLLATE NOCASE",
    ]
    entity_binds: list[Any] = [effective_session_key, name]
    if entity_type_filter:
        entity_filters.append("e.entity_type = ?")
        entity_binds.append(entity_type_filter)

    entity_sql = (
        f"{VISIBLE_MENTIONS_CTE}{entity_agg_cte(include_first_in=True)}\n"
        "           SELECT e.entity_id, e.session_key, e.canonical_text, e.entity_type,\n"
        "                  ea.first_at AS first_seen_at,\n"
        "                  ea.last_at  AS last_seen_at,\n"
        "                  ea.first_in AS first_seen_in_summary_id,\n"
        "                  ea.occ_count AS occurrence_count,\n"
        "                  ea.visible_surfaces AS alternate_surfaces,\n"
        "                  e.metadata\n"
        "             FROM lcm_entities e\n"
        "             JOIN entity_agg ea ON ea.entity_id = e.entity_id\n"
        f"            WHERE {' AND '.join(entity_filters)}\n"
        "            LIMIT 1"
    )

    raw_entity = ctx.conn.execute(entity_sql, entity_binds).fetchone()
    entity = _row_to_entity(raw_entity)

    if entity is None:
        # ----- 6. Not-found branch (TS lines 225-246) ---------------------
        # The "not found" branch covers BOTH "no such entity" AND
        # "all mentions suppressed" — they're indistinguishable to the
        # agent by design (operator suppression is the contract). The
        # message intentionally doesn't say "or has been suppressed" so
        # an attacker can't infer entity existence by querying.
        type_note = f" of type '{entity_type_filter}'" if entity_type_filter else ""
        # Concrete fallbacks the agent can try right now (Eva onboarding
        # feedback: empty entity result should suggest next steps, not
        # dead-end). Try in order: prefix browse → paraphrastic search.
        prefix_token = name.lower().replace("-", " ").replace("_", " ").split()
        first_token = prefix_token[0] if prefix_token else name
        return tool_result(
            {
                "found": False,
                "name": name,
                "sessionKey": effective_session_key,
                "entityType": entity_type_filter,
                "message": (
                    f"No entity matching '{name}'{type_note} in "
                    f"session_key='{effective_session_key}'. The entity "
                    "coreference worker may not have run yet, or the name "
                    "doesn't appear in any leaf summary."
                ),
                "fallback_suggestions": [
                    (
                        f"lcm_search_entities query='{first_token}' "
                        "mode='prefix' — browse entities by canonical-name "
                        "prefix (handles 'Smarter-Claw' vs 'smarter claw' "
                        "canonicalization mismatches)"
                    ),
                    (
                        f"lcm_grep mode='hybrid' pattern='{name}' — "
                        "paraphrastic search across all summary content "
                        "(works without an entity catalog entry, surfaces "
                        "mentions even if coreference hasn't run)"
                    ),
                    (
                        f"lcm_grep mode='verbatim' pattern='{name}' — "
                        "exact-text search of source messages (for citation "
                        "/ quote-back use cases)"
                    ),
                ],
            },
        )

    # ----- 7. Mention list (TS lines 249-263) -----------------------------
    # JOIN summaries so we filter suppressed leaves at the query level
    # (matches TS lines 251-257). Order by mentioned_at DESC, cap by
    # mentionLimit.
    mention_sql = (
        "SELECT m.mention_id, m.entity_id, m.summary_id, m.surface_form,\n"
        "       m.span_start, m.span_end, m.mentioned_at\n"
        "  FROM lcm_entity_mentions m\n"
        "  JOIN summaries s ON s.summary_id = m.summary_id\n"
        "  WHERE m.entity_id = ?\n"
        "    AND s.suppressed_at IS NULL\n"
        "  ORDER BY m.mentioned_at DESC\n"
        "  LIMIT ?"
    )
    raw_mentions = ctx.conn.execute(
        mention_sql,
        (entity.entity_id, mention_limit),
    ).fetchall()
    mentions = [_row_to_mention(r) for r in raw_mentions]

    # ----- 8. Compute alternate-surfaces display + metadata (TS lines 270-274) -----
    # LCM Wave-12 P1 (2026-04): alternate_surfaces is now the recomputed
    # distinct set from unsuppressed mentions only. Strip the canonical
    # form so the list shows only *alternate* surfaces (matches the
    # column's intent + parity with stored representation).
    # Original: lossless-claw/src/tools/lcm-get-entity-tool.ts:265-273.
    all_surfaces = _safe_json_parse_list(entity.alternate_surfaces)
    alt_surfaces = [
        s
        for s in all_surfaces
        # Case-insensitive comparison matches TS localeCompare(..., {sensitivity:"base"}).
        if s.casefold() != entity.canonical_text.casefold()
    ]
    metadata = _safe_json_parse_dict(entity.metadata)

    # ----- 9. Render markdown (TS lines 276-311) --------------------------
    lines: list[str] = []
    lines.append(f"## Entity: {entity.canonical_text}")
    lines.append("")
    lines.append(f"- **Type**: {entity.entity_type}")
    lines.append(f"- **Entity ID**: `{entity.entity_id}`")
    lines.append(f"- **Session key**: `{entity.session_key}`")
    lines.append(f"- **First seen**: {_format_display_time(entity.first_seen_at, ctx.timezone)}")
    lines.append(f"- **Last seen**: {_format_display_time(entity.last_seen_at, ctx.timezone)}")
    lines.append(f"- **Total occurrences**: {entity.occurrence_count}")
    if alt_surfaces:
        lines.append(f"- **Alternate surfaces**: {', '.join(alt_surfaces)}")
    if entity.first_seen_in_summary_id:
        lines.append(f"- **First seen in**: `{entity.first_seen_in_summary_id}`")
    lines.append("")
    if len(mentions) == 0:
        lines.append("_No agent-visible mentions (all may be in suppressed leaves)._")
    else:
        # LCM Wave-12 P1 (2026-04): occurrence_count is now visible-only,
        # so len(mentions) == occurrence_count when not truncated by
        # mentionLimit. Show "(N of M)" only when truncation actually
        # happened.
        # Original: lossless-claw/src/tools/lcm-get-entity-tool.ts:295-304.
        truncated = len(mentions) < entity.occurrence_count
        if truncated:
            lines.append(f"### Mentions ({len(mentions)} of {entity.occurrence_count})")
        else:
            lines.append(f"### Mentions ({len(mentions)})")
        lines.append("")
        for m in mentions:
            lines.append(
                f"- [{_format_display_time(m.mentioned_at, ctx.timezone)}] "
                f'in `{m.summary_id}` — surface: "{m.surface_form}"',
            )

    # ----- 10. Build payload (TS lines 313-337) ---------------------------
    payload: dict[str, Any] = {
        "text": "\n".join(lines),
        "details": {
            "found": True,
            "entityId": entity.entity_id,
            "name": entity.canonical_text,
            "entityType": entity.entity_type,
            "sessionKey": entity.session_key,
            "firstSeenAt": entity.first_seen_at,
            "lastSeenAt": entity.last_seen_at,
            "totalOccurrences": entity.occurrence_count,
            "alternateSurfaces": alt_surfaces,
            "firstSeenInSummaryId": entity.first_seen_in_summary_id,
            "metadata": metadata,
            "mentions": [
                {
                    "mentionId": m.mention_id,
                    "summaryId": m.summary_id,
                    "surfaceForm": m.surface_form,
                    "spanStart": m.span_start,
                    "spanEnd": m.span_end,
                    "mentionedAt": m.mentioned_at,
                }
                for m in mentions
            ],
            "mentionsTruncated": len(mentions) == mention_limit,
        },
    }
    return tool_result(payload)


# ===========================================================================
# Row adapters
# ===========================================================================


def _row_to_entity(raw: Any) -> Optional[_EntityRow]:
    """Adapt a sqlite3 row (Row or tuple) to :class:`_EntityRow`.

    Returns ``None`` if ``raw`` is ``None``. Used by the entity-lookup
    branch — when ``raw is None`` the handler emits the "not found"
    payload.

    The adapter accepts both :class:`sqlite3.Row` (when the connection
    has ``row_factory = sqlite3.Row``) and plain tuples (default
    factory). For tuples we rely on the SELECT-column order from
    :func:`handle_lcm_get_entity`'s entity_sql query.
    """
    if raw is None:
        return None
    if isinstance(raw, sqlite3.Row):
        return _EntityRow(
            entity_id=raw["entity_id"],
            session_key=raw["session_key"],
            canonical_text=raw["canonical_text"],
            entity_type=raw["entity_type"],
            first_seen_at=raw["first_seen_at"],
            last_seen_at=raw["last_seen_at"],
            first_seen_in_summary_id=raw["first_seen_in_summary_id"],
            occurrence_count=int(raw["occurrence_count"] or 0),
            alternate_surfaces=raw["alternate_surfaces"],
            metadata=raw["metadata"],
        )
    # Tuple fallback — order from entity_sql SELECT.
    return _EntityRow(
        entity_id=raw[0],
        session_key=raw[1],
        canonical_text=raw[2],
        entity_type=raw[3],
        first_seen_at=raw[4],
        last_seen_at=raw[5],
        first_seen_in_summary_id=raw[6],
        occurrence_count=int(raw[7] or 0),
        alternate_surfaces=raw[8],
        metadata=raw[9],
    )


def _row_to_mention(raw: Any) -> _MentionRow:
    """Adapt a sqlite3 row (Row or tuple) to :class:`_MentionRow`.

    See :func:`_row_to_entity` for the dual-format rationale. SELECT
    column order from :func:`handle_lcm_get_entity`'s mention_sql.
    """
    if isinstance(raw, sqlite3.Row):
        return _MentionRow(
            mention_id=raw["mention_id"],
            entity_id=raw["entity_id"],
            summary_id=raw["summary_id"],
            surface_form=raw["surface_form"],
            span_start=raw["span_start"],
            span_end=raw["span_end"],
            mentioned_at=raw["mentioned_at"],
        )
    return _MentionRow(
        mention_id=raw[0],
        entity_id=raw[1],
        summary_id=raw[2],
        surface_form=raw[3],
        span_start=raw[4],
        span_end=raw[5],
        mentioned_at=raw[6],
    )


# ===========================================================================
# JSON parsing helpers (TS lines 99-106)
# ===========================================================================


def _safe_json_parse_list(s: Optional[str]) -> list[str]:
    """Parse a JSON string as a ``list[str]`` or return ``[]`` on failure.

    Mirrors TS ``safeJsonParse<string[]>`` — null / empty / parse error
    all collapse to the empty list. Non-string list items are filtered
    out (defensive coercion against accidentally-stored mixed lists).
    """
    if not s:
        return []
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def _safe_json_parse_dict(s: Optional[str]) -> dict[str, Any]:
    """Parse a JSON string as a ``dict`` or return ``{}`` on failure.

    Mirrors TS ``safeJsonParse<Record<string, unknown>>``. Non-dict
    values collapse to ``{}``.
    """
    if not s:
        return {}
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


# ===========================================================================
# Time formatter (TS lines 92-97)
# ===========================================================================


def _format_display_time(value: Any, timezone_name: str) -> str:
    """Format a timestamp for display in entity output.

    Accepts :class:`datetime`, string, number (epoch), ``None``, or
    invalid input. Returns ``"-"`` for missing / unparseable input,
    otherwise a ``YYYY-MM-DD HH:MM TZ`` string per the LCM
    :func:`_format_timestamp` convention. Mirrors TS
    ``formatDisplayTime`` lines 92-97 (which delegates to
    ``formatTimestamp`` in compaction.ts).
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
