"""LCM agent tools — `/lcm` slash-command handlers + shared helpers.

Ports the surface of ``lossless-claw/src/tools/`` (LCM commit ``1f07fbd``
on branch ``pr-613``) to Python.

This package is the home of the seven `/lcm` agent tools plus the
non-tool shared SQL/conversation/recursion helpers that several of them
import. The tool handlers themselves arrive in Epic 06; this package
also hosts the entity-synthesis shared helpers in Epic 07.

### Public API as of issue 07-01

* :data:`entity_shared.VISIBLE_MENTIONS_CTE` — module-level SQL
  constant, the suppression-aware ``WITH visible_mentions AS (...)``
  clause shared by ``lcm_get_entity`` and ``lcm_search_entities``.
* :func:`entity_shared.entity_agg_cte` — builds the
  ``, entity_agg AS (...)`` CTE fragment that recomputes per-entity
  aggregates from visible (unsuppressed) mentions only.

Both come from ``lossless-claw/src/tools/lcm-entity-shared.ts`` — the
Wave-12 reviewer F4 fix that extracted the byte-identical CTE pair out
of the two consumer tools to close the parallel-edit drift hazard.

See also:

* ``docs/porting-guides/tools.md`` §"lcm-entity-shared.ts" lines 582-588
* ``docs/adr/029-wave-fix-provenance.md`` Wave-12 row (F4 sibling fix)
"""

from __future__ import annotations

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

__all__ = [
    "LcmConversationScope",
    "LcmDependencies",
    "VISIBLE_MENTIONS_CTE",
    "entity_agg_cte",
    "parse_iso_timestamp_param",
    "resolve_lcm_conversation_scope",
]
