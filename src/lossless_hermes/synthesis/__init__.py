"""LCM v4.1 synthesis layer.

The synthesis subsystem turns leaf summaries into condensed / yearly /
custom-window summaries via tier-appropriate prompt templates. The
pieces ported in epic 07:

* ``types`` (issue 07-08) — shared :class:`MemoryType` / :class:`PassKind`
  literal aliases, the :class:`PromptRecord` dataclass, and
  :exc:`PromptRegistryError`. Lives in its own module so
  :mod:`prompt_registry` and :mod:`dispatch` can both import without a
  circular reference.
* ``prompt_registry`` (issue 07-08) — append-only versioning over
  :sql:`lcm_prompt_registry`.
* ``seed_prompts`` (issue 07-08) — idempotent seeding of the §12
  default prompt rows so :func:`dispatch_synthesis` does not return
  ``missing_prompt`` errors on first call.
* ``dispatch`` (issue 07-05) — tier-aware synthesis dispatcher
  (:class:`SynthesisDispatcher`). Defines the canonical
  :class:`LlmCall` Protocol consumed by the dispatcher; the entity
  extractor in :mod:`lossless_hermes.extraction.extractor` defines an
  equivalent :class:`LlmCompleteFn` Protocol with the same shape.

Issues 07-06 (cache) and 07-09 (audit) build on this foundation. The
TS canonical source (commit ``1f07fbd`` on branch ``pr-613``) is
:file:`lossless-claw/src/synthesis/`.
"""

from __future__ import annotations

from lossless_hermes.synthesis.dispatch import (
    DEFAULT_MODEL_BY_TIER,
    HARD_CAP_BEST_OF_N,
    PASS_STRATEGY_BY_TIER,
    BestOfNDetail,
    LlmCall,
    LlmCallArgs,
    LlmCallResult,
    SynthesisDispatcher,
    SynthesisDispatchError,
    SynthesizeRequest,
    SynthesizeResult,
    TierLabel,
    dispatch_synthesis,
)
from lossless_hermes.synthesis.prompt_registry import (
    PromptRegistryError,
    RegisterPromptOptions,
    bump_bundle_version,
    get_active_prompt,
    get_prompt_by_id,
    list_active_prompts,
    register_prompt,
)
from lossless_hermes.synthesis.seed_prompts import (
    DEFAULT_PROMPTS,
    SeedResult,
    seed_default_prompts,
)
from lossless_hermes.synthesis.types import (
    MemoryType,
    PassKind,
    PromptRecord,
)

__all__ = [
    "DEFAULT_MODEL_BY_TIER",
    "DEFAULT_PROMPTS",
    "HARD_CAP_BEST_OF_N",
    "PASS_STRATEGY_BY_TIER",
    "BestOfNDetail",
    "LlmCall",
    "LlmCallArgs",
    "LlmCallResult",
    "MemoryType",
    "PassKind",
    "PromptRecord",
    "PromptRegistryError",
    "RegisterPromptOptions",
    "SeedResult",
    "SynthesisDispatchError",
    "SynthesisDispatcher",
    "SynthesizeRequest",
    "SynthesizeResult",
    "TierLabel",
    "bump_bundle_version",
    "dispatch_synthesis",
    "get_active_prompt",
    "get_prompt_by_id",
    "list_active_prompts",
    "register_prompt",
    "seed_default_prompts",
]
