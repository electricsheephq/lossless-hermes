"""Port of ``lcm_search_entities`` — entity catalog search / browse tool.

Ports ``lossless-claw/src/tools/lcm-search-entities-tool.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 377 LOC TS → ~330 LOC Python). The
TypeBox-declared schema lives at TS lines 43-83; the handler body at
lines 152-374. Both are translated structurally verbatim per ADR-016
(description prose byte-identical from TS source).

What this tool does
-------------------

``lcm_search_entities`` is the **PRIMARY tool for entity discovery /
browse** — when the agent doesn't know the canonical name yet, or wants
to enumerate what's in the catalog. It is the entity-side sibling of
``lcm_get_entity`` (which is the exact-name lookup path).

The TS source documents three modes funneled through a single tool:

1. **browse by type** — pass ``entityType`` (e.g. ``"pr_number"``,
   ``"person_name"``, ``"file_path"``) with no query to list all
   entities of that type. Useful for "what PRs have we discussed?",
   "what kinds of entities are in this corpus?".
2. **fuzzy lookup** — pass partial / approximate ``query`` with
   ``mode='like'`` (default substring) or ``mode='prefix'``. Useful for
   "I'm looking for that customer with the VM issues, can't remember
   the exact name" or "show me anything starting with Voy".
3. **catalog probe** — empty-query + entityType filter to enumerate
   (overlaps with mode 1).

Returns ranked entities (occurrence_count DESC, last_seen DESC) with
their type + occurrence count + last-seen time. Once the agent has a
canonical name, it follows up with ``lcm_get_entity`` for the full
mention list.

Backed by ``lcm_entities`` (populated by the async entity-coreference
worker, ports landing under issue 07-*).

Wave-N invariants (preserved per ADR-029)
-----------------------------------------

Multiple Wave-N fixes converge here; all preserved verbatim:

* **Wave-1 (extractor canonical-type vocabulary)** — the schema's
  ``entityType`` field documents the snake_case canonical-type list
  (``person_name``, ``pr_number``, ``agent_id``, ``session_key``,
  ``command``, ``file_path``, ``date``). Earlier docs incorrectly listed
  ``person``/``project``/``pr`` which never matched. The Wave-1 #7
  finding fixes this in the schema description prose.
* **Wave-10 reviewer P2** — EXISTS guard requires at least one
  unsuppressed mention. Without it, entities with all-suppressed
  mentions leaked via search even though ``lcm_get_entity`` properly
  filtered them. The ``VISIBLE_MENTIONS_CTE`` join also acts as a
  visible-mentions gate, but the explicit EXISTS guard is defense in
  depth.
* **Wave-12 reviewer P1 (F4 sibling)** — rank + display aggregates from
  unsuppressed mentions only (mirrors ``lcm_get_entity``). Without the
  CTE, ``occurrence_count`` + ``last_seen_at`` + ``alternate_surfaces``
  leaked suppressed-mention data, ranking biased toward
  heavily-suppressed entities, and surface forms first introduced in
  suppressed leaves remained visible.
* **Wave-12 consolidation B** — CTE extracted into the shared
  :mod:`entity_shared` helper to close the parallel-edit drift hazard
  with ``lcm_get_entity`` (both maintain byte-identical SQL).
  ``search_entities`` calls ``entity_agg_cte(include_first_in=False)``
  because it doesn't need ``first_seen_in_summary_id``.
* **Wave-12 consolidation (browse-by-type)** — allow empty ``query``
  when ``entityType`` is set so the agent can enumerate by type.
  Otherwise ``query`` is still required — empty query + no entityType
  is too broad to be useful.
* **P8 harness fix (2026-05-06)** — empty result triggers a
  three-state ``catalogStatus`` probe so callers (and the agent) know
  which scenario they are in: ``active`` (query just didn't match),
  ``empty-for-session`` (worker hasn't run on this session),
  ``empty-globally`` (worker hasn't run on this DB at all). This is
  critical UX — the agent must know "entity doesn't exist" vs "worker
  hasn't run yet".
* **Audit 3 finding #3** — use ``EXISTS(SELECT 1 ... LIMIT 1)`` instead
  of ``COUNT(*)`` for the catalog-probe queries to avoid full-table
  scans on multi-million-entity DBs.
* **Wave-12 F5 (middleware-not-decorator)** — per
  :mod:`lossless_hermes.plugin.needs_compact_gate`, this handler is the
  INNER handler and the ``run_with_token_gate`` wrap happens at the
  dispatch layer (issue 06-02 — ``LCMEngine.handle_tool_call``). The
  wrap MUST happen at invocation time, NOT at registration time.

Architecture seams
------------------

The handler does NOT depend on :class:`LCMEngine` directly — instead it
takes a narrow :class:`SearchEntitiesContext` Protocol that exposes:

* ``conn: sqlite3.Connection`` — for the SQL prepared statements.
* ``conversation_store: ConversationStore`` — for the conversation
  scope resolver via the ``_LcmLike`` Protocol shape.
* ``timezone: str`` — passed through to the timestamp formatter.

Source map
----------

* TS canonical: ``lossless-claw/src/tools/lcm-search-entities-tool.ts:1-377``.
* Porting guide: ``docs/porting-guides/tools.md`` §"lcm_search_entities"
  lines 444-490.
* Issue spec: ``epics/06-tools/06-11-lcm-search-entities.md``.
* ADR-016 — TypeBox hand-translate policy (description prose
  byte-identical).
* ADR-029 — Wave-N provenance comments at preserved fix sites.
* TS test fixture: ``test/lcm-search-entities-tool.test.ts`` (394 LOC).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Optional, Protocol

from lossless_hermes.store.conversation import ConversationStore
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
    "LCM_SEARCH_ENTITIES_DESCRIPTION",
    "LCM_SEARCH_ENTITIES_SCHEMA",
    "SearchEntitiesContext",
    "escape_like",
    "handle_lcm_search_entities",
)


# ===========================================================================
# Constants — match TS lines 37-39 verbatim
# ===========================================================================

_DEFAULT_LIMIT: Final[int] = 20
"""Default ``limit`` when the caller omits it (TS line 37)."""

_MIN_LIMIT: Final[int] = 1
"""Minimum allowed ``limit`` (TS line 38)."""

_MAX_LIMIT: Final[int] = 100
"""Maximum allowed ``limit`` (TS line 39)."""


# ===========================================================================
# Schema — verbatim from TS source (ADR-016 §Consequences)
# ===========================================================================
#
# The tool-level description string and per-field description strings are
# byte-identical to ``lcm-search-entities-tool.ts:137-150`` (tool desc) and
# 43-83 (per-field). The mechanical TypeBox → dict translation uses the
# helpers in ``_typebox.py``.

LCM_SEARCH_ENTITIES_DESCRIPTION: Final[str] = (
    "PRIMARY tool for entity discovery / browse — use when you DON'T know "
    "the canonical name yet, or want to see what's in the catalog. "
    "Three use modes covered by this single tool: "
    "(1) **browse by type**: pass `entityType` (e.g. 'pr_number', 'person_name', 'file_path') "
    "with no query to list all entities of a type — useful for 'what PRs have we discussed?', "
    "'what kinds of entities are in this corpus?'; "
    "(2) **fuzzy lookup**: pass partial / approximate `query` with `mode='like'` (default, "
    "substring) or `mode='prefix'` — useful for 'I'm looking for that customer with the VM "
    "issues, can't remember the exact name' or 'show me anything starting with Voy'; "
    "(3) **catalog probe**: empty-query + entityType filter to enumerate. "
    "Returns ranked entities (occurrence_count DESC, last_seen DESC) with their type + "
    "occurrence count + last-seen time. Once you have a canonical name, follow up with "
    "`lcm_get_entity` for the full mention list. Backed by the async entity coreference worker."
)
"""Verbatim from ``lcm-search-entities-tool.ts:137-150``. Per ADR-016
§Consequences this is the load-bearing model-facing prose that drives
tool selection — the three-modes routing copy ("browse by type", "fuzzy
lookup", "catalog probe") is the agent's mental model for when to reach
for this tool."""


LCM_SEARCH_ENTITIES_SCHEMA: Final[dict[str, Any]] = tool_schema(
    name="lcm_search_entities",
    description=LCM_SEARCH_ENTITIES_DESCRIPTION,
    parameters=object_schema(
        query=optional(
            string_field(
                "Search query (substring by default; use `mode` to switch to 'prefix' or 'exact'). "
                "All matches are COLLATE NOCASE. "
                "OPTIONAL when `entityType` is provided — empty query + entityType browses all "
                "entities of a given type (e.g. browse all PRs by setting entityType='pr_number'). "
                "REQUIRED when entityType is absent.",
            ),
        ),
        mode=optional(
            string_field(
                "Match mode (default 'like'). 'prefix' matches start; 'exact' matches whole.",
                enum=["like", "prefix", "exact"],
            ),
        ),
        sessionKey=optional(
            string_field(
                "Session key scope. If omitted, defaults to the current session's key.",
            ),
        ),
        entityType=optional(
            string_field(
                "Optional entity_type filter. Common values produced by the entity-coreference extractor: "
                "'person_name', 'pr_number', 'agent_id', 'session_key', 'command', 'file_path', 'date'. "
                "Wave-1 Auditor #7 finding #8: the extractor uses snake_case canonical types; older docs "
                "incorrectly listed 'person'/'project'/'pr' which never matched. Use lcm_search_entities "
                "without an entityType filter first to discover what's actually in the catalog.",
            ),
        ),
        limit=optional(
            number_field(
                f"Max entities to return (default {_DEFAULT_LIMIT}; range {_MIN_LIMIT}-{_MAX_LIMIT}).",
                minimum=_MIN_LIMIT,
                maximum=_MAX_LIMIT,
            ),
        ),
    ),
)
"""OpenAI-function-call schema for ``lcm_search_entities``. Verbatim
translation of the TypeBox declaration at
``lcm-search-entities-tool.ts:43-83`` per ADR-016."""


# Register at module import time per the TOOL_SCHEMAS contract documented
# in tools/__init__.py. The 06-02 dispatch table reads via
# ``get_tool_schemas()`` so this side-effect is what makes the tool
# discoverable to the LCMEngine.
TOOL_SCHEMAS.append(LCM_SEARCH_ENTITIES_SCHEMA)


# ===========================================================================
# SearchEntitiesContext — narrow Protocol exposing what the handler needs
# ===========================================================================


class SearchEntitiesContext(Protocol):
    """The handler's collaborator surface.

    Mirrors the slice of :class:`~lossless_hermes.engine.LCMEngine` that
    ``lcm_search_entities`` actually needs. Using a structural Protocol
    keeps the handler decoupled from the engine class shape and lets
    tests construct a tiny stand-in dataclass.

    Required attributes:

    * ``conn``: :class:`sqlite3.Connection` for the prepared statements.
    * ``conversation_store``: :class:`ConversationStore` — present for
      symmetry with ``lcm_describe`` and ``lcm_get_entity``. The TS
      source does not consult the conversation store for
      ``lcm_search_entities`` (the entity catalog is keyed by
      ``session_key``, not ``conversation_id``), but exposing the store
      here makes the Protocol drop-in compatible with the eventual
      :class:`LCMEngine` in case a future fix needs scope resolution.
    * ``timezone``: IANA timezone name for the timestamp formatter
      (e.g. ``"UTC"``, ``"America/Los_Angeles"``).
    """

    conn: sqlite3.Connection
    conversation_store: ConversationStore
    timezone: str


# ===========================================================================
# escape_like — defensive LIKE-pattern escape
# ===========================================================================


def escape_like(value: str) -> str:
    """Escape ``%``, ``_``, and ``\\`` for use in a SQL LIKE pattern.

    Ports TS ``escapeLike`` (lines 111-115). Without this, user-supplied
    ``%`` or ``_`` characters widen the search inadvertently:

    * ``query="100%pure"`` would match ``100abc`` (the ``%`` is a
      wildcard).
    * ``query="abc_def"`` would match ``abcXdef`` (the ``_`` matches any
      single character).

    The escaped pattern is bound via SQLite's ``ESCAPE '\\'`` clause —
    callers MUST append ``ESCAPE '\\'`` to the LIKE expression so the
    backslash is interpreted as the escape character rather than a
    literal backslash.

    Order matters: backslash MUST be escaped first; otherwise the later
    ``\\%`` / ``\\_`` insertions would themselves be re-escaped.

    Args:
        value: The raw user query string. Caller is responsible for
            stripping / lowercasing as needed (this helper preserves
            case and whitespace verbatim — only the three metacharacters
            are transformed).

    Returns:
        The escaped string, safe to splice into ``LIKE ?`` with
        ``ESCAPE '\\'`` appended.

    Examples:
        >>> escape_like("100%pure")
        '100\\\\%pure'
        >>> escape_like("abc_def")
        'abc\\\\_def'
        >>> escape_like("plain")
        'plain'
        >>> escape_like("a\\\\b")
        'a\\\\\\\\b'
    """
    # Backslash MUST be first — see docstring rationale.
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ===========================================================================
# _normalize_mode — coerce caller-supplied mode to the enum
# ===========================================================================


def _normalize_mode(value: Any) -> str:
    """Coerce ``value`` to one of ``{'like', 'prefix', 'exact'}``.

    Ports TS ``normalizeMode`` (lines 117-120). Unknown / missing values
    fall through to ``'like'``. The schema enum gates this at the
    provider side, but the runtime helper is defensive in case a
    provider emits a different string (or a non-string).
    """
    if value == "prefix" or value == "exact":
        return value
    return "like"


# ===========================================================================
# Handler entry point
# ===========================================================================


def handle_lcm_search_entities(  # noqa: PLR0912, PLR0915 — mirrors TS structure
    args: dict[str, Any],
    *,
    ctx: SearchEntitiesContext,
    session_key: Optional[str] = None,
) -> str:
    """Handle an ``lcm_search_entities`` tool call.

    **Wave-12 F5 invariant:** this is the INNER handler. The
    ``run_with_token_gate`` middleware MUST wrap this call at the
    dispatch layer (issue 06-02 — ``LCMEngine.handle_tool_call``); see
    the module docstring's "Wave-N invariants" section. The wrap MUST
    happen at invocation time, NOT at registration time (decorator-time
    computation would freeze the gate state).

    Args:
        args: The tool-call ``arguments`` dict from the LLM provider.
            Read defensively — see :mod:`lossless_hermes.tools._common`.
        ctx: A :class:`SearchEntitiesContext` exposing the SQL +
            conversation-store + timezone collaborator surface.
        session_key: Optional session-family key for scope. The
            ``sessionKey`` arg in ``args`` (caller-supplied) overrides
            this default when present.

    Returns:
        A JSON string per the :func:`tool_result` contract.

    Tool-error payloads (returned as JSON strings):

    * Empty query AND no entityType: ``{"error": "`query` is required..."}``.
    * No session key resolved: ``{"error": "No session_key resolved..."}``.
    """
    # ----- Param read + validation -----------------------------------------
    # TS line 166: query is read as a trimmed string; empty after trim is
    # treated as absent. Use the project's defensive read_string_param.
    query_raw = read_string_param(args, "query")
    query = query_raw if query_raw is not None else ""

    # TS lines 167-170: entityType trimmed + lowercased; empty -> None.
    entity_type_raw = read_string_param(args, "entityType")
    entity_type_filter: Optional[str] = (
        entity_type_raw.lower() if entity_type_raw is not None else None
    )

    # LCM Wave-12 (2026-04): allow empty query when entityType is set
    # (browse-by-type use case). Otherwise query is still required —
    # empty query + no entityType is too broad to be useful.
    # Original: lossless-claw/src/tools/lcm-search-entities-tool.ts:171-179.
    if len(query) == 0 and entity_type_filter is None:
        return tool_result(
            {
                "error": (
                    "`query` is required (non-empty), unless you provide `entityType` "
                    "to browse all entities of a type (e.g. entityType='pr_number' "
                    "lists all known PRs)."
                ),
            },
        )

    mode = _normalize_mode(args.get("mode"))

    # ----- Session-key resolution ------------------------------------------
    # TS lines 183-190: prefer caller-supplied sessionKey (the args dict),
    # fall back to the input.sessionKey passed by the engine wiring.
    session_key_param_raw = read_string_param(args, "sessionKey")
    session_key_param = session_key_param_raw if session_key_param_raw is not None else ""

    effective_session_key: Optional[str]
    if session_key_param:
        effective_session_key = session_key_param
    elif isinstance(session_key, str) and session_key.strip():
        effective_session_key = session_key.strip()
    else:
        effective_session_key = None

    if not effective_session_key:
        return tool_result(
            {
                "error": (
                    "No session_key resolved. Pass `sessionKey` explicitly or "
                    "call from an active LCM session."
                ),
            },
        )

    # ----- Limit clamp (TS lines 194-197) ----------------------------------
    # TS uses Math.max(MIN, Math.min(MAX, Math.trunc(p.limit))) for finite
    # numbers, default otherwise. The Python helper read_number_param
    # clamps + handles non-numerics for us; we then truncate to int.
    limit_raw = read_number_param(
        args,
        "limit",
        minimum=_MIN_LIMIT,
        maximum=_MAX_LIMIT,
        default=_DEFAULT_LIMIT,
    )
    # read_number_param returns float | None; we always supplied a default
    # so the None case is unreachable, but typecheckers don't see that.
    limit = int(limit_raw if limit_raw is not None else _DEFAULT_LIMIT)

    # ----- Build SQL filters + binds (TS lines 208-243) --------------------
    db = ctx.conn

    # LCM Wave-10 (2026-03): EXISTS guard requires at least one mention
    # whose summary is not suppressed. Without it, entities with
    # all-suppressed mentions leaked via search even though lcm_get_entity
    # properly filtered them. Defense in depth alongside the
    # VISIBLE_MENTIONS_CTE join (the CTE also filters, but the EXISTS
    # guard is the explicit contract). Wave-10 reviewer P2 fix.
    # Original: lossless-claw/src/tools/lcm-search-entities-tool.ts:208-217.
    filters: list[str] = [
        "e.session_key = ?",
        # EXISTS guard: at least one mention whose summary is not suppressed.
        (
            "EXISTS (\n"
            "           SELECT 1 FROM lcm_entity_mentions m\n"
            "             JOIN summaries s ON s.summary_id = m.summary_id\n"
            "             WHERE m.entity_id = e.entity_id\n"
            "               AND s.suppressed_at IS NULL\n"
            "         )"
        ),
    ]
    binds: list[Any] = [effective_session_key]

    # LCM Wave-12 (2026-04): empty query is allowed in the browse-by-type
    # case (entityType filter must be present, validated upstream). Skip
    # the LIKE/exact predicate when query is empty so we don't add
    # ``e.canonical_text LIKE '%%'`` (matches everything but obfuscates
    # intent).
    # Original: lossless-claw/src/tools/lcm-search-entities-tool.ts:220-239.
    if len(query) > 0:
        if mode == "prefix":
            escaped = escape_like(query)
            filters.append("e.canonical_text LIKE ? ESCAPE '\\' COLLATE NOCASE")
            binds.append(f"{escaped}%")
        elif mode == "exact":
            filters.append("e.canonical_text = ? COLLATE NOCASE")
            binds.append(query)
        else:  # 'like' (default substring)
            escaped = escape_like(query)
            filters.append("e.canonical_text LIKE ? ESCAPE '\\' COLLATE NOCASE")
            binds.append(f"%{escaped}%")

    if entity_type_filter:
        filters.append("e.entity_type = ?")
        binds.append(entity_type_filter)

    # ----- Build main SELECT + execute (TS lines 259-275) ------------------
    # LCM Wave-12 (2026-04): rank + display aggregates from unsuppressed
    # mentions only (mirrors lcm_get_entity). Without the CTE,
    # occurrence_count + last_seen_at + alternate_surfaces leaked
    # suppressed-mention data, ranking biased toward heavily-suppressed
    # entities, and surface forms first introduced in suppressed leaves
    # remained visible. The CTE join also acts as the visible-mentions
    # gate (no unsuppressed mention -> entity hidden, mirroring the
    # EXISTS guard above). Wave-12 reviewer P1 / F4-sibling fix.
    # Wave-12 consolidation B: CTE extracted into shared helper
    # (``entity_shared.py``) to close the parallel-edit drift hazard
    # with get-entity. search-entities omits ``first_in`` (it's the
    # get-entity-only ``first_seen_in_summary_id`` column) by passing
    # ``include_first_in=False``.
    # Original: lossless-claw/src/tools/lcm-search-entities-tool.ts:245-275.
    sql = (
        f"{VISIBLE_MENTIONS_CTE}{entity_agg_cte(include_first_in=False)}\n"
        "           SELECT e.entity_id, e.canonical_text, e.entity_type,\n"
        "                  ea.first_at AS first_seen_at,\n"
        "                  ea.last_at  AS last_seen_at,\n"
        "                  ea.occ_count AS occurrence_count,\n"
        "                  ea.visible_surfaces AS alternate_surfaces\n"
        "             FROM lcm_entities e\n"
        "             JOIN entity_agg ea ON ea.entity_id = e.entity_id\n"
        f"            WHERE {' AND '.join(filters)}\n"
        "            ORDER BY ea.occ_count DESC, ea.last_at DESC\n"
        "            LIMIT ?"
    )
    rows = list(db.execute(sql, [*binds, limit]).fetchall())

    # ----- catalogStatus probe (TS lines 286-303) --------------------------
    # P8 fix (2026-05-06 harness): distinguish "0 results for query" from
    # "0 entities indexed yet" — the latter is a coverage gap, not a
    # negative answer. Probe the catalog scope so callers (and the agent)
    # know which scenario they're in.
    # Audit 3 finding #3 fix: use EXISTS(SELECT 1 ... LIMIT 1) instead of
    # COUNT(*) to avoid full-table scans on multi-million-entity DBs.
    # EXISTS short-circuits at the first row it finds (or doesn't) and
    # is O(log n) via the lcm_entities_lookup_idx index when filtered by
    # session_key, O(1) on the global probe.
    # Original: lossless-claw/src/tools/lcm-search-entities-tool.ts:286-303.
    catalog_status: str = "active"
    if len(rows) == 0:
        session_exists_row = db.execute(
            "SELECT EXISTS(SELECT 1 FROM lcm_entities WHERE session_key = ? LIMIT 1) AS e",
            (effective_session_key,),
        ).fetchone()
        session_exists = _row_int(session_exists_row, "e", 0)
        if session_exists == 0:
            global_exists_row = db.execute(
                "SELECT EXISTS(SELECT 1 FROM lcm_entities LIMIT 1) AS e",
            ).fetchone()
            global_exists = _row_int(global_exists_row, "e", 0)
            catalog_status = "empty-globally" if global_exists == 0 else "empty-for-session"

    # ----- Render markdown lines (TS lines 306-341) ------------------------
    timezone_name = ctx.timezone
    entities_payload: list[dict[str, Any]] = []
    for raw_row in rows:
        row = _row_to_dict(raw_row)
        canonical_text = str(row.get("canonical_text", ""))
        # alternate_surfaces is the JSON-array text from
        # json_group_array(DISTINCT vm.surface_form). Parse + strip
        # canonical (TS lines 357-359 — the recomputed list captures all
        # distinct forms incl. canonical itself).
        all_surfaces = _safe_json_parse_list(row.get("alternate_surfaces"))
        alt_surfaces = [s for s in all_surfaces if s.casefold() != canonical_text.casefold()]
        entities_payload.append(
            {
                "entityId": str(row.get("entity_id", "")),
                "canonicalText": canonical_text,
                "entityType": str(row.get("entity_type", "")),
                "firstSeenAt": row.get("first_seen_at"),
                "lastSeenAt": row.get("last_seen_at"),
                "occurrenceCount": int(row.get("occurrence_count", 0) or 0),
                "alternateSurfaces": alt_surfaces,
            },
        )

    lines: list[str] = []
    lines.append("## LCM Entity Search")
    lines.append("")
    lines.append(f"- **Query**: `{query}` (mode: {mode})")
    lines.append(f"- **Session key**: `{effective_session_key}`")
    if entity_type_filter:
        lines.append(f"- **Type filter**: {entity_type_filter}")
    limit_reached = len(rows) == limit
    matches_suffix = (
        f" (limit {limit} reached — narrow with mode='prefix' or entityType)"
        if limit_reached
        else ""
    )
    lines.append(f"- **Matches**: {len(rows)}{matches_suffix}")
    lines.append("")

    if len(rows) == 0:
        if catalog_status == "empty-globally":
            lines.append(
                "_No entities indexed in this DB at all. The entity-coreference worker "
                "has not run on this DB. This is a coverage gap, NOT a negative answer "
                "to your query — the entity may exist in the corpus but has not been "
                "extracted yet. Fall back to lcm_grep --mode hybrid for now._"
            )
        elif catalog_status == "empty-for-session":
            lines.append(
                f"_No entities indexed for session_key `{effective_session_key}` "
                "(other sessions DO have entities — the worker has run on the corpus "
                "but not on this session yet, or no extractable entities have appeared "
                "in its leaves). Try sessionKey='agent:main:main' if you intended the "
                "main thread, or fall back to lcm_grep._"
            )
        else:
            lines.append(
                "_No entities matched this query (the catalog has entries for this "
                "session, but none match — try a wider query, mode='like', or drop the "
                "entityType filter)._"
            )
    else:
        lines.append("| Entity | Type | Occurrences | Last seen |")
        lines.append("|---|---|---|---|")
        for entity in entities_payload:
            lines.append(
                f"| **{entity['canonicalText']}** | {entity['entityType']} | "
                f"{entity['occurrenceCount']} | "
                f"{_format_display_time(entity['lastSeenAt'], timezone_name)} |"
            )
        lines.append("")
        lines.append(
            "Use `lcm_get_entity({ name: '<canonical>' })` for the full mention list "
            "of any entry above."
        )

    # ----- Final payload (TS lines 343-372) --------------------------------
    payload: dict[str, Any] = {
        "text": "\n".join(lines),
        "query": query,
        "mode": mode,
        "sessionKey": effective_session_key,
        "entityType": entity_type_filter,
        "totalMatches": len(rows),
        "limitReached": limit_reached,
        "catalogStatus": catalog_status,
        "entities": entities_payload,
    }
    return tool_result(payload)


# ===========================================================================
# Row + JSON helpers
# ===========================================================================


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert a sqlite3 row (tuple or :class:`sqlite3.Row`) to a dict.

    Both row types support index/key access, but only :class:`sqlite3.Row`
    supports keyword access. Callers may run against either based on
    whether the connection has ``row_factory = sqlite3.Row`` set, so we
    handle both.
    """
    if isinstance(row, sqlite3.Row):
        return {k: row[k] for k in row.keys()}
    if isinstance(row, dict):
        return dict(row)
    # Tuple — caller must ensure they fetched the right columns in order.
    # We fall back to a generic conversion; this path is mainly defensive.
    return {}


def _row_int(row: Any, key: str, default: int) -> int:
    """Read an integer column from a sqlite3 row (Row or tuple).

    The catalog-probe queries select a single ``AS e`` column, so either
    the Row's keyword path or the tuple's index path works. We try
    keyword first, then fall back to index 0.
    """
    if row is None:
        return default
    try:
        if isinstance(row, sqlite3.Row):
            return int(row[key])
        if isinstance(row, dict):
            return int(row[key])
        # Tuple fallback
        return int(row[0])
    except (TypeError, ValueError, IndexError, KeyError):
        return default


def _safe_json_parse_list(value: Any) -> list[str]:
    """Parse a JSON-array-of-strings text safely.

    Mirrors TS ``safeJsonParse<string[]>`` (lines 102-109). Returns an
    empty list on any failure (missing column, empty string, non-JSON,
    non-array, non-string elements).
    """
    if not isinstance(value, str) or not value:
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    # Keep only string elements; defensive in case the JSON has nulls.
    return [s for s in parsed if isinstance(s, str)]


# ===========================================================================
# Timestamp formatting (mirrors describe.py for consistency)
# ===========================================================================


def _format_display_time(value: Any, timezone_name: str) -> str:
    """Format a timestamp for display in search output. Mirrors TS lines 95-100.

    Accepts :class:`datetime`, string, number (epoch), ``None``, or
    invalid input. Returns ``"-"`` for missing / unparseable input,
    otherwise a ``YYYY-MM-DD HH:MM TZ`` string via the LCM
    :func:`_format_timestamp` helper.
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
    # Lazy import to dodge a top-level cycle with compaction.py.
    from lossless_hermes.compaction import _format_timestamp  # noqa: PLC0415

    try:
        return _format_timestamp(dt, timezone_name)
    except (TypeError, ValueError):
        return "-"


# ===========================================================================
# Test seam — minimal in-tree dataclass that satisfies SearchEntitiesContext
# ===========================================================================
#
# The Protocol above is structural; this concrete dataclass exists so
# tests don't have to redeclare the shape.


@dataclass
class _SearchCtx:
    """Concrete :class:`SearchEntitiesContext` for tests / wiring.

    Production callers wire :class:`LCMEngine` directly (it exposes the
    same attributes structurally). Tests build this dataclass with a
    SQLite connection + a :class:`ConversationStore` + a timezone string.
    """

    conn: sqlite3.Connection
    conversation_store: ConversationStore
    timezone: str
