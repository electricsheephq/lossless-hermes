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
F4 fix that extracted the byte-identical CTE pair shared by
``lcm_get_entity`` and ``lcm_search_entities``).

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

* :data:`entity_shared.VISIBLE_MENTIONS_CTE` — module-level SQL
  constant, the suppression-aware ``WITH visible_mentions AS (...)``
  clause shared by ``lcm_get_entity`` and ``lcm_search_entities``.
* :func:`entity_shared.entity_agg_cte` — builds the
  ``, entity_agg AS (...)`` CTE fragment that recomputes per-entity
  aggregates from visible (unsuppressed) mentions only.

Both come from ``lossless-claw/src/tools/lcm-entity-shared.ts`` — the
Wave-12 reviewer F4 fix that extracted the byte-identical CTE pair out
of the two consumer tools to close the parallel-edit drift hazard.

References
----------

* [ADR-016: TypeBox -> JSON Schema translation](../../docs/adr/016-typebox-translation.md)
* [docs/typebox-translation.md](../../docs/typebox-translation.md)
* [docs/porting-guides/tools.md](../../docs/porting-guides/tools.md)
* docs/porting-guides/tools.md §"lcm-entity-shared.ts" lines 582-588
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
from lossless_hermes.tools.entity_shared import (
    VISIBLE_MENTIONS_CTE,
    entity_agg_cte,
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

    Notes:
        Per ADR-024 §Project layout the engine class shell lives in
        ``src/lossless_hermes/engine/__init__.py`` and its
        ``get_tool_schemas`` method delegates here. The engine is
        decoupled from which tools have been ported — adding a new
        tool only requires creating the per-tool module and importing
        it (which appends to :data:`TOOL_SCHEMAS`).
    """
    # Return a shallow copy: callers should NOT be able to mutate the
    # registry by accident. The contents are not deep-copied — the
    # schemas are intentionally read-only data, and a shallow copy is
    # enough to prevent .append/.pop on the registry list.
    return list(TOOL_SCHEMAS)


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------


__all__: Final = (
    "OptionalField",
    "TOOL_SCHEMAS",
    "VISIBLE_MENTIONS_CTE",
    "array_field",
    "boolean_field",
    "entity_agg_cte",
    "get_tool_schemas",
    "number_field",
    "object_schema",
    "optional",
    "string_field",
    "tool_schema",
    "validate_schema",
)
