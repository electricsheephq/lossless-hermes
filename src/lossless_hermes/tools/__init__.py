"""LCM agent tools — schema definitions, dispatch handlers, shared helpers.

This package owns the eight LCM tools (``lcm_grep``, ``lcm_describe``,
``lcm_expand``, ``lcm_expand_query``, ``lcm_synthesize_around``,
``lcm_get_entity``, ``lcm_search_entities``, ``lcm_compact``). Each
per-tool module exports an ``LCM_<TOOL>_SCHEMA`` dict (OpenAI
function-call format) and a handler callable. Issue 06-01 lands the
**foundation**: the TypeBox -> Python dict translation helpers
(:mod:`lossless_hermes.tools._typebox`) and the schema registry
(:func:`get_tool_schemas` below). Per-tool ports land in 06-07..06-14.

The package also hosts non-tool shared SQL/conversation/recursion
helpers that several tools import — e.g. :mod:`entity_shared` (Wave-12
F4 fix, issue 07-01), :mod:`conversation_scope` (issue 06-05),
:mod:`expansion_recursion_guard` (issue 06-06).

Per [ADR-016](../../docs/adr/016-typebox-translation.md), schemas are
**hand-translated** from the TypeScript source (not auto-generated).
The decision rejected automated generation because TypeBox description
prose is load-bearing — it's the model-facing text that drives
tool-selection behavior, tuned across 12 audit waves. An automated
converter would silently paraphrase or re-escape strings, degrading
that behavior.

Per [ADR-029](../../docs/adr/029-wave-fix-provenance.md), every
``LCM_<TOOL>_SCHEMA`` dict carries a provenance comment at the top::

    # Verbatim from src/tools/lcm-<tool>-tool.ts:<line range>
    LCM_GREP_SCHEMA = {...}

This is a contract for future readers: when LCM bumps and a schema
description shifts, the line range in the comment points to the
upstream change so the diff can be reviewed deliberately.

Registry pattern
----------------

Per-tool modules append their schema to :data:`TOOL_SCHEMAS` at import
time. :func:`get_tool_schemas` returns the list. Wiring in 06-02
(``LCMEngine.get_tool_schemas``) delegates to this function so the
engine doesn't need to know which tools have been ported.

Until per-tool issues land (06-07..06-14), :data:`TOOL_SCHEMAS` is
empty — :func:`get_tool_schemas` returns ``[]``. The well-formedness
test in ``tests/tools/test_schemas_wellformed.py`` is parametrized
over the registry, so it auto-extends as schemas are added.

Entity-shared helpers (from issue 07-01)
----------------------------------------

* :data:`entity_shared.VISIBLE_MENTIONS_CTE` — suppression-aware
  ``WITH visible_mentions AS (...)`` clause shared by ``lcm_get_entity``
  and ``lcm_search_entities``.
* :func:`entity_shared.entity_agg_cte` — builds the ``, entity_agg AS (...)``
  CTE fragment.

Conversation-scope helpers (from issue 06-05)
---------------------------------------------

* :class:`conversation_scope.LcmConversationScope` — dataclass describing
  resolved scope (one conversation / family / all).
* :class:`conversation_scope.LcmDependencies` — narrow Protocol slice for
  the resolver's collaborator surface.
* :func:`conversation_scope.parse_iso_timestamp_param` — TS-parity ISO
  timestamp parser.
* :func:`conversation_scope.resolve_lcm_conversation_scope` — 5-priority
  scope resolver (matches TS 92-161).

Expansion recursion guard (from issue 06-06)
--------------------------------------------

* :class:`expansion_recursion_guard.DelegatedExpansionContext` — stamped
  metadata on a delegated child session.
* :func:`expansion_recursion_guard.evaluate_expansion_recursion_guard` —
  the depth-cap / idempotent-reentry decision used by ``lcm_expand`` and
  the deferred ``lcm_expand_query`` (per ADR-012).
* :func:`expansion_recursion_guard.acquire_expansion_concurrency_slot` /
  ``release_expansion_concurrency_slot`` — per-origin in-flight slot.

References
----------

* [ADR-016: TypeBox -> JSON Schema translation](../../docs/adr/016-typebox-translation.md)
* [docs/typebox-translation.md](../../docs/typebox-translation.md)
* [docs/porting-guides/tools.md](../../docs/porting-guides/tools.md)
* docs/adr/029-wave-fix-provenance.md Wave-12 row (F4 sibling fix)
"""

from __future__ import annotations

from typing import Any, Final

from lossless_hermes.tools._typebox import (
    OptionalField,
    array_field,
    boolean_field,
    number_field,
    object_schema,
    optional,
    string_field,
    tool_schema,
    validate_schema,
)
from lossless_hermes.tools.conversation_scope import (
    LcmConversationScope,
    LcmDependencies,
    parse_iso_timestamp_param,
    resolve_lcm_conversation_scope,
)
from lossless_hermes.tools.entity_shared import (
    VISIBLE_MENTIONS_CTE,
    entity_agg_cte,
)
from lossless_hermes.tools.expansion_recursion_guard import (
    EXPANSION_CONCURRENCY_ERROR_CODE,
    EXPANSION_DELEGATION_DEPTH_CAP,
    EXPANSION_RECURSION_ERROR_CODE,
    DelegatedExpansionContext,
    ExpansionConcurrencyGuardDecision,
    ExpansionRecursionGuardDecision,
    acquire_expansion_concurrency_slot,
    clear_delegated_expansion_context,
    create_expansion_request_id,
    evaluate_expansion_recursion_guard,
    record_expansion_delegation_telemetry,
    release_expansion_concurrency_slot,
    resolve_expansion_request_id,
    resolve_next_expansion_depth,
    stamp_delegated_expansion_context,
)

# ---------------------------------------------------------------------------
# Tool-schema registry
# ---------------------------------------------------------------------------
#
# Per-tool modules append their schemas here at import time. Wiring in
# issue 06-02 (LCMEngine.get_tool_schemas) returns get_tool_schemas().
#
# Empty at issue 06-01: per-tool ports populate this list as they land
# (issues 06-07 through 06-14). The well-formedness test auto-extends.

TOOL_SCHEMAS: Final[list[dict[str, Any]]] = []


# Per-tool modules register their schemas at import time by appending to
# ``TOOL_SCHEMAS``. Import them here AFTER ``TOOL_SCHEMAS`` is defined so
# the registry exists before the per-tool module runs its top-level
# ``.append(...)`` call. The import is at the bottom of this section so
# the helper exports above (TypeBox builders, conversation scope, etc.)
# are already available to the per-tool modules.
#
# Ordering note: import order = registration order. Tests rely on the
# tool list being stable; adding a new per-tool module appends; the
# 06-02 dispatch table sees the same order.
from lossless_hermes.tools import describe as _describe  # noqa: F401, E402
from lossless_hermes.tools import get_entity as _get_entity  # noqa: F401, E402
from lossless_hermes.tools import search_entities as _search_entities  # noqa: F401, E402


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return the registered LCM tool schemas (OpenAI function-call format).

    Per-tool modules (``tools/grep.py``, ``tools/describe.py``, …)
    register their ``LCM_<TOOL>_SCHEMA`` dict at import time via
    appending to :data:`TOOL_SCHEMAS`. The order is the import order
    of those modules — currently empty (issues 06-07..06-14 land the
    real schemas).

    Returns:
        A FRESH list (so callers can mutate it freely without
        affecting the registry). Each entry has the shape
        ``{"name": ..., "description": ..., "parameters": ...}``.
    """
    return list(TOOL_SCHEMAS)


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------


__all__: Final = (
    "EXPANSION_CONCURRENCY_ERROR_CODE",
    "EXPANSION_DELEGATION_DEPTH_CAP",
    "EXPANSION_RECURSION_ERROR_CODE",
    "DelegatedExpansionContext",
    "ExpansionConcurrencyGuardDecision",
    "ExpansionRecursionGuardDecision",
    "LcmConversationScope",
    "LcmDependencies",
    "OptionalField",
    "TOOL_SCHEMAS",
    "VISIBLE_MENTIONS_CTE",
    "acquire_expansion_concurrency_slot",
    "array_field",
    "boolean_field",
    "clear_delegated_expansion_context",
    "create_expansion_request_id",
    "entity_agg_cte",
    "evaluate_expansion_recursion_guard",
    "get_tool_schemas",
    "number_field",
    "object_schema",
    "optional",
    "parse_iso_timestamp_param",
    "record_expansion_delegation_telemetry",
    "release_expansion_concurrency_slot",
    "resolve_expansion_request_id",
    "resolve_lcm_conversation_scope",
    "resolve_next_expansion_depth",
    "stamp_delegated_expansion_context",
    "string_field",
    "tool_schema",
    "validate_schema",
)
