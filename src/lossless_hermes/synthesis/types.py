"""Shared types for the synthesis layer (issue 07-08).

Lives in its own module so :mod:`prompt_registry`, the forthcoming
:mod:`dispatch` (issue 07-05), and :mod:`seed_prompts` can all import
the literal-string type aliases + :class:`PromptRecord` dataclass
without a circular reference.

Ports the TypeScript ``MemoryType`` / ``PassKind`` literal unions and
``PromptRecord`` interface from ``lossless-claw/src/synthesis/prompt-registry.ts``
(commit ``1f07fbd``, lines 31-53).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Literal-string type aliases (mirror TS unions in prompt-registry.ts:31-39)
# ---------------------------------------------------------------------------

MemoryType = Literal[
    "episodic-leaf",
    "episodic-condensed",
    "episodic-yearly",
    "procedural-extract",
    "entity-extract",
    "theme-consolidation",
]
"""Memory-type discriminator stored in :sql:`lcm_prompt_registry.memory_type`.

The six values mirror the CHECK constraint on the column (see
``src/lossless_hermes/db/migration.py`` ``_SQL_TABLE_LCM_PROMPT_REGISTRY``).
"""


PassKind = Literal["single", "verify_fidelity", "best_of_n_judge"]
"""Pass-kind discriminator on :sql:`lcm_prompt_registry.pass_kind`.

* ``single`` — the primary synthesis pass (one prompt → one output).
* ``verify_fidelity`` — second pass that checks the primary output's
  claims against the source bundle (monthly tier only at present).
* ``best_of_n_judge`` — third pass that picks the best of N parallel
  primary outputs (yearly tier only at present).
"""


# ---------------------------------------------------------------------------
# PromptRecord — value object returned from registry lookups
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromptRecord:
    """One row of :sql:`lcm_prompt_registry`.

    Returned by :func:`prompt_registry.get_active_prompt`,
    :func:`prompt_registry.get_prompt_by_id`, and
    :func:`prompt_registry.list_active_prompts`. Frozen so callers cannot
    mutate the in-memory copy and create false-confidence drift versus
    the DB row.

    Attribute names match the Python ``snake_case`` convention; the TS
    interface uses ``camelCase`` (see ``prompt-registry.ts:41-53``). The
    mapping is documented in :func:`prompt_registry._row_to_record`.
    """

    prompt_id: str
    memory_type: MemoryType
    tier_label: str | None
    pass_kind: PassKind
    version: int
    template: str
    model_recommendation: str | None
    created_at: str
    active: bool
    bundle_version: int
    notes: str | None
