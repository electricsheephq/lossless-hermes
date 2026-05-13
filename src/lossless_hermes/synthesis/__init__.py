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
* ``cache_key`` (issue 07-06) — 7-field cache-key derivation +
  single-flight INSERT-OR-IGNORE for :sql:`lcm_synthesis_cache`.
  Centralises the Wave-10 ``tier_label + prompt_id`` widening so
  callers cannot accidentally pick a different shape.
* ``tier_routing`` (issue 07-10) — public surface for the tier-to-model
  routing policy decided in ADR-031 (Option A: match TS exactly, single
  env var, deferred ladder). Re-exports the tier-defaults table +
  pass-strategy table under stable names and exposes a public
  :func:`pick_synthesis_model` wrapper over dispatch's private
  ``_pick_model``.
* ``invalidation`` (issue 07-07) — best-effort
  :func:`record_cache_leaf_refs` populate + caller-owned-tx
  :func:`invalidate_caches_for_suppressed_leaves` DELETE for the
  :sql:`lcm_cache_leaf_refs` inverse index. Closes the soft-purge leak
  (Final.review.3 Loop 2 Leak 2.5) where ``ON DELETE CASCADE`` did not
  fire on the ``UPDATE suppressed_at`` write, so post-suppression cache
  reads could surface PII baked in before suppression.
* ``audit`` (issue 07-09) — INSERT/UPDATE wrappers for the
  :sql:`lcm_synthesis_audit` forensic trail plus the two retention
  sweeps (orphan ``'started'`` rows, 30-day-old terminal rows).
  :class:`SynthesisDispatcher` delegates here for every pass.

The TS canonical source (commit ``1f07fbd`` on branch ``pr-613``) is
:file:`lossless-claw/src/synthesis/`.
"""

from __future__ import annotations

from lossless_hermes.synthesis.audit import (
    AUDIT_ID_PREFIX,
    AUDIT_MAX_LEN,
    AUDIT_TRUNCATED_MARKER,
    DEFAULT_RETENTION_DAYS,
    ENV_RETENTION_DAYS,
    LAST_ERROR_MAX_LEN,
    AuditCompletedResult,
    AuditInsertContext,
    generate_audit_id,
    insert_audit_started,
    resolve_retention_days,
    sweep_orphan_audit_starts,
    sweep_terminal_audit_rows,
    truncate_for_audit,
    update_audit_completed,
    update_audit_failed,
)
from lossless_hermes.synthesis.cache_key import (
    DEFAULT_SESSION_KEY,
    LEAF_FINGERPRINT_HEX_LEN,
    CacheKey,
    CacheRowInsertResult,
    ExistingCacheRow,
    InvalidLeafIdError,
    generate_cache_id,
    insert_cache_row_single_flight,
    leaf_fingerprint,
    lookup_cache_row,
    resolve_session_key,
)
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
from lossless_hermes.synthesis.invalidation import (
    invalidate_caches_for_suppressed_leaves,
    record_cache_leaf_refs,
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
from lossless_hermes.synthesis.tier_routing import (
    DEFAULT_SUMMARY_MODEL_FALLBACK,
    LCM_SUMMARY_MODEL_ENV,
    SYNTHESIS_TIER_DEFAULTS,
    SYNTHESIS_TIER_PASS_STRATEGIES,
    TIER_LADDER_DEFERRED,
    pick_synthesis_model,
    resolve_default_model_from_env,
)
from lossless_hermes.synthesis.types import (
    MemoryType,
    PassKind,
    PromptRecord,
)

__all__ = [
    "AUDIT_ID_PREFIX",
    "AUDIT_MAX_LEN",
    "AUDIT_TRUNCATED_MARKER",
    "DEFAULT_MODEL_BY_TIER",
    "DEFAULT_PROMPTS",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_SESSION_KEY",
    "DEFAULT_SUMMARY_MODEL_FALLBACK",
    "ENV_RETENTION_DAYS",
    "HARD_CAP_BEST_OF_N",
    "LAST_ERROR_MAX_LEN",
    "LCM_SUMMARY_MODEL_ENV",
    "LEAF_FINGERPRINT_HEX_LEN",
    "PASS_STRATEGY_BY_TIER",
    "SYNTHESIS_TIER_DEFAULTS",
    "SYNTHESIS_TIER_PASS_STRATEGIES",
    "TIER_LADDER_DEFERRED",
    "AuditCompletedResult",
    "AuditInsertContext",
    "BestOfNDetail",
    "CacheKey",
    "CacheRowInsertResult",
    "ExistingCacheRow",
    "InvalidLeafIdError",
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
    "generate_audit_id",
    "generate_cache_id",
    "get_active_prompt",
    "get_prompt_by_id",
    "insert_audit_started",
    "insert_cache_row_single_flight",
    "invalidate_caches_for_suppressed_leaves",
    "leaf_fingerprint",
    "list_active_prompts",
    "lookup_cache_row",
    "pick_synthesis_model",
    "record_cache_leaf_refs",
    "register_prompt",
    "resolve_default_model_from_env",
    "resolve_retention_days",
    "resolve_session_key",
    "seed_default_prompts",
    "sweep_orphan_audit_starts",
    "sweep_terminal_audit_rows",
    "truncate_for_audit",
    "update_audit_completed",
    "update_audit_failed",
]
