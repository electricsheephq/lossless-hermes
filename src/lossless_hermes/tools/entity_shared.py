"""Shared SQL CTE fragments for entity-aggregate queries.

Ports ``lossless-claw/src/tools/lcm-entity-shared.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 84 LOC) to Python.

Two exports — :data:`VISIBLE_MENTIONS_CTE` (a SQL constant) and
:func:`entity_agg_cte` (a SQL builder) — used by ``lcm_get_entity``
and ``lcm_search_entities`` (Epic 06) plus the entity-coreference
extraction worker's read paths (Epic 07).

### Why this exists (Wave-12 reviewer F4 + consolidation methodology B)

Both entity-facing tools must compute their aggregates from
**unsuppressed mentions only**, otherwise suppressed-mention data
leaks via aggregate columns:

* ``occurrence_count`` over-counts (counts mentions whose parent
  summary has been suppressed by the operator).
* ``first_seen_at`` / ``last_seen_at`` reveal that suppressed leaves
  exist (the row-level counter on ``lcm_entities`` is producer-side
  and does not decrement on suppression — by design, see 07-02).
* ``alternate_surfaces`` includes surface forms whose only mentions
  are in suppressed leaves.

The TS Wave-12 F4 fix landed the suppression-aware CTE in both
``lcm-get-entity-tool.ts`` and ``lcm-search-entities-tool.ts`` via
parallel edits — byte-identical SQL maintained in two places, a
parallel-edit drift hazard.

The architectural-decision methodology (Step 3 adversarial review,
2026-05-08) chose Option B (extract shared helper) over Option A
(merge tools into ``lcm_entity { mode }``) because:

* Both adversarial agents independently recommended B.
* Reach-for / usage telemetry is unavailable to validate the merge.
* B is reversible to A when telemetry arrives.

The producer-side counters on ``lcm_entities`` are written by the
coreference worker (07-02) and never decremented on suppression —
that's the "lossless" half of lossless-bedrock. The agent-surface
rectification lives here: every ``/lcm`` read MUST go through these
CTEs. Only operator-internal tooling that legitimately needs the raw
counters reads ``lcm_entities`` directly.

### Source map

* TS canonical: ``lossless-claw/src/tools/lcm-entity-shared.ts:1-84``
* TS consumers (post-port): ``tools/lcm-get-entity-tool.ts:210`` +
  ``tools/lcm-search-entities-tool.ts`` (Epic 06 ports).
* Porting guide: ``docs/porting-guides/tools.md`` §"lcm-entity-shared.ts"
  lines 582-588.
* Issue spec: ``epics/07-entity-synthesis/07-01-entity-shared-cte.md``

### No SQL injection surface

Neither export interpolates caller-provided text. ``entity_agg_cte``'s
sole parameter is a Python ``bool`` that toggles between two static
SQL fragments — the boolean itself never reaches the SQL string. The
callers parameterize their main ``SELECT`` body with ``?`` bind
parameters (session_key, entity_type filter, etc.), which is the
correct SQLite practice and orthogonal to this CTE pair.
"""

from __future__ import annotations

# Byte-equivalent port of TS template literals. Both constants must
# remain whitespace-stable so the parity test against the vendored TS
# fixture continues to pass — Wave-12 F4's whole point was to keep the
# SQL byte-identical across consumers.
#
# Implementation note (Python ↔ JS template-literal parity):
# JS backticks preserve the leading newline after the opening backtick
# and any embedded indentation. Python triple-quoted strings behave the
# same way as long as we start the content on a fresh line — which we
# do, so this is a literal byte-for-byte transcription of the TS source.

# ---------------------------------------------------------------------------
# VISIBLE_MENTIONS_CTE — suppression-aware visible-mentions filter.
# ---------------------------------------------------------------------------
#
# Joins ``lcm_entity_mentions`` to ``summaries`` and filters out mentions
# whose parent summary is suppressed. Pure SQL, no parameters.
#
# Callers append ``, entity_agg AS (...)`` (built via ``entity_agg_cte``)
# and then their main ``SELECT`` body referencing ``entity_agg ea``.
#
# Equivalent TS template:
#
#     export const VISIBLE_MENTIONS_CTE = `
#       WITH visible_mentions AS (
#         SELECT m.entity_id, m.summary_id, m.surface_form, m.mentioned_at
#           FROM lcm_entity_mentions m
#           JOIN summaries s ON s.summary_id = m.summary_id
#          WHERE s.suppressed_at IS NULL
#       )
#     `;
VISIBLE_MENTIONS_CTE = """
  WITH visible_mentions AS (
    SELECT m.entity_id, m.summary_id, m.surface_form, m.mentioned_at
      FROM lcm_entity_mentions m
      JOIN summaries s ON s.summary_id = m.summary_id
     WHERE s.suppressed_at IS NULL
  )
"""


# ---------------------------------------------------------------------------
# entity_agg_cte — per-entity aggregates from visible mentions.
# ---------------------------------------------------------------------------


def entity_agg_cte(*, include_first_in: bool) -> str:
    """Build the ``, entity_agg AS (...)`` CTE clause.

    Composes after :data:`VISIBLE_MENTIONS_CTE`. Computes per-entity
    aggregates from the visible-mentions filter (suppressed mentions
    excluded):

    * ``occ_count`` — ``COUNT(*)`` of unsuppressed mentions.
    * ``first_at`` / ``last_at`` — ``MIN`` / ``MAX`` of ``mentioned_at``.
    * ``first_in`` (optional) — first visible ``summary_id`` per entity,
      ordered by ``(mentioned_at ASC, summary_id ASC)``. Tie-break by
      ``summary_id`` keeps the ordering deterministic when two mentions
      share a timestamp.
    * ``visible_surfaces`` — distinct surface forms as a JSON array
      via ``json_group_array(DISTINCT ...)``.

    Args:
        include_first_in: When ``True``, include the ``first_in``
            subquery column. ``lcm_get_entity`` surfaces this as
            ``first_seen_in_summary_id`` so the agent can cite the
            origin leaf. ``lcm_search_entities`` doesn't need it and
            passes ``False`` to skip the per-row correlated subquery.

    Returns:
        SQL fragment starting with a literal ``, `` so it concatenates
        cleanly after :data:`VISIBLE_MENTIONS_CTE`. The output is
        whitespace-stable — bytes match the TS template-literal output
        for the same ``include_first_in`` value.

    The sole parameter is a Python ``bool`` — never interpolated into
    SQL — so this helper has no SQL-injection surface. Callers
    parameterize their main ``SELECT`` body with ``?`` binds for
    session_key, entity_type, etc.
    """

    if include_first_in:
        # 14 leading spaces on the continuation lines match the TS
        # template-literal indentation — see test_entity_shared.py for
        # the parity assertion.
        first_in_expr = (
            "(SELECT vm2.summary_id\n"
            "              FROM visible_mentions vm2\n"
            "              WHERE vm2.entity_id = vm.entity_id\n"
            "              ORDER BY vm2.mentioned_at ASC, vm2.summary_id ASC\n"
            "              LIMIT 1) AS first_in,"
        )
    else:
        first_in_expr = ""

    # Mirrors the TS template:
    #
    #     return `, entity_agg AS (
    #         SELECT
    #           vm.entity_id,
    #           COUNT(*) AS occ_count,
    #           MIN(vm.mentioned_at) AS first_at,
    #           MAX(vm.mentioned_at) AS last_at,
    #           ${firstInExpr}
    #           json_group_array(DISTINCT vm.surface_form) AS visible_surfaces
    #          FROM visible_mentions vm
    #         GROUP BY vm.entity_id
    #       )`;
    #
    # When ``first_in_expr == ""`` the ${firstInExpr} line collapses to
    # 6 leading spaces + newline (a blank-ish line). This matches the JS
    # template-literal substitution exactly — the test fixture covers it.
    return (
        f", entity_agg AS (\n"
        f"    SELECT\n"
        f"      vm.entity_id,\n"
        f"      COUNT(*) AS occ_count,\n"
        f"      MIN(vm.mentioned_at) AS first_at,\n"
        f"      MAX(vm.mentioned_at) AS last_at,\n"
        f"      {first_in_expr}\n"
        f"      json_group_array(DISTINCT vm.surface_form) AS visible_surfaces\n"
        f"     FROM visible_mentions vm\n"
        f"    GROUP BY vm.entity_id\n"
        f"  )"
    )


__all__ = [
    "VISIBLE_MENTIONS_CTE",
    "entity_agg_cte",
]
