"""Synthesis tier-to-model routing — public API + Option A policy marker (issue 07-10).

This module is the **public surface** for the synthesis tier-routing policy
decided in :doc:`ADR-031 </adr/031-synthesis-tier-model-routing>`. It wraps
:mod:`lossless_hermes.synthesis.dispatch`'s private :func:`_pick_model` and
:data:`DEFAULT_MODEL_BY_TIER` so callers (operator commands, health-check
output, future override surfaces) can read the routing state **without
importing private dispatch internals**.

### Why this module exists alongside ``dispatch.py``

``dispatch.py`` already encodes the precedence logic in :func:`_pick_model`
(private to dispatch). The Wave-4 Auditor #5 P1 fix is well-tested via
:class:`tests.synthesis.test_dispatch.TestModelResolution` +
:class:`TestParityChecklist.test_2_force_model_no_override_uses_tier_default`.
What's missing is a **stable, public, audit-friendly** surface for the same
behaviour that:

* Lets ``/lcm health`` (issue 08-03) report the resolved default model name
  without grovelling through ``synthesis.dispatch`` internals.
* Documents the **deferred-decision** state of the tier ladder so a future
  contributor reading the code finds the ADR-031 link inline (the same
  refactor-resistance pattern ADR-029 uses for Wave-N comments).
* Provides a single import path (``lossless_hermes.synthesis.tier_routing``)
  for the routing policy — convenient for future Option B/C migrations that
  may need to call into this surface from outside ``synthesis/``.

### Option A semantics (per ADR-031)

* The TS source (:file:`lossless-claw/src/synthesis/dispatch.ts:71-79`,
  commit ``1f07fbd`` on branch ``pr-613``) reads ``process.env.LCM_SUMMARY_MODEL``
  once at module load with a ``"gpt-5.4-mini"`` fallback, and populates
  every tier's default with that single value.
* The Python port mirrors this exactly in
  :mod:`lossless_hermes.synthesis.dispatch`'s
  :data:`DEFAULT_MODEL_BY_TIER`. This module re-exports it under the
  public name :data:`SYNTHESIS_TIER_DEFAULTS` to make caller intent clear.
* Operator override path for per-tier tuning is
  :func:`lossless_hermes.synthesis.prompt_registry.register_prompt` with a
  non-None ``model_recommendation``. Per-call override is via
  :attr:`SynthesizeRequest.model_override` and :attr:`SynthesizeRequest.force_model`.

### Forward-reference marker

:data:`TIER_LADDER_DEFERRED` is the inline marker for ADR-031's "open
deferral" — a future grep across the source for the constant name surfaces
every place that's aware of the deferral. Mirrors ADR-029's pattern for
Wave-N markers but applied to a single forward-looking decision.

### Source pin

* TS canonical: :file:`lossless-claw/src/synthesis/dispatch.ts:71-79` (env
  resolution + per-tier defaults table) and ``:755-766`` (``pickModel``
  precedence).
* ADR-031: :doc:`/adr/031-synthesis-tier-model-routing` — the decision +
  rationale.
* Spec: :file:`epics/07-entity-synthesis/07-10-tier-routing.md`.
"""

from __future__ import annotations

import os
from typing import Final

from lossless_hermes.synthesis.dispatch import (
    DEFAULT_MODEL_BY_TIER,
    PASS_STRATEGY_BY_TIER,
    SynthesizeRequest,
    TierLabel,
    _pick_model,
)
from lossless_hermes.synthesis.types import PassKind, PromptRecord

__all__ = [
    "DEFAULT_SUMMARY_MODEL_FALLBACK",
    "LCM_SUMMARY_MODEL_ENV",
    "SYNTHESIS_TIER_DEFAULTS",
    "SYNTHESIS_TIER_PASS_STRATEGIES",
    "TIER_LADDER_DEFERRED",
    "pick_synthesis_model",
    "resolve_default_model_from_env",
]


#: Environment variable name that holds the global default synthesis model.
#: Matches the TS source ``process.env.LCM_SUMMARY_MODEL`` (``dispatch.ts:71``).
#: Same variable is used by the existing leaf summarizer in
#: :mod:`lossless_hermes.summarize`, so an operator setting it once flows
#: through both the hot-path summarizer and worker-side synthesis dispatch.
LCM_SUMMARY_MODEL_ENV: Final[str] = "LCM_SUMMARY_MODEL"


#: Fallback model identifier when :data:`LCM_SUMMARY_MODEL_ENV` is unset or
#: blank. Matches the TS string literal ``"gpt-5.4-mini"`` at
#: ``dispatch.ts:71``. Note this name is NOT an Anthropic model id; the
#: Hermes-side adapter (Epic 04) decides what to do with it on dispatch
#: (pass through and 4xx, or rewrite to a known-good fallback). Operator is
#: expected to set :data:`LCM_SUMMARY_MODEL_ENV` on a fresh install — README
#: calls this out.
DEFAULT_SUMMARY_MODEL_FALLBACK: Final[str] = "gpt-5.4-mini"


#: Public alias for :data:`lossless_hermes.synthesis.dispatch.DEFAULT_MODEL_BY_TIER`.
#:
#: All six tiers default to the same model (Option A per ADR-031). The
#: model identifier is resolved at ``dispatch.py`` module-import time from
#: :data:`LCM_SUMMARY_MODEL_ENV` with :data:`DEFAULT_SUMMARY_MODEL_FALLBACK`
#: as the fallback. Tier-specific tuning is operator-driven via
#: :sql:`lcm_prompt_registry.model_recommendation`, not by hardcoded
#: ladders.
#:
#: Re-exported here for callers that want the policy surface
#: (``health``, ``/lcm status``, future override CLIs) without importing
#: from the implementation module.
SYNTHESIS_TIER_DEFAULTS: Final[dict[TierLabel, str]] = DEFAULT_MODEL_BY_TIER


#: Public alias for :data:`lossless_hermes.synthesis.dispatch.PASS_STRATEGY_BY_TIER`.
#:
#: Tier → pass-kind list. Each tier runs the passes in listed order.
#: ``"yearly"`` expands ``"best_of_n_judge"`` inside dispatch to N candidate
#: single-pass calls + one judge call.
#:
#: Re-exported for operator/health visibility — callers can render this map
#: to explain why a tier dispatches K LLM calls without reading dispatch.py.
SYNTHESIS_TIER_PASS_STRATEGIES: Final[dict[TierLabel, list[PassKind]]] = PASS_STRATEGY_BY_TIER


#: Forward-reference marker for ADR-031 "tier ladder deferred to Epic 09 eval".
#:
#: Per ADR-031: the Python port matches TS exactly in v0.1 — single env
#: var, NULL ``model_recommendation`` in seed. The opinionated haiku →
#: sonnet → opus + extended-thinking ladder from
#: :file:`docs/porting-guides/synthesis.md` §"Tier model" is **deferred**
#: until Epic 09 eval data exists. This constant is the inline marker so
#: that ``grep -rn "TIER_LADDER_DEFERRED" src/`` enumerates every place the
#: deferral is consulted or referenced.
#:
#: Operator path for opting into a tier ladder before v0.2:
#:
#: .. code-block:: python
#:
#:     from lossless_hermes.synthesis.prompt_registry import register_prompt, RegisterPromptOptions
#:     register_prompt(
#:         db,
#:         RegisterPromptOptions(
#:             memory_type="episodic-yearly",
#:             tier_label="yearly",
#:             pass_kind="best_of_n_judge",
#:             template=existing_template,
#:             model_recommendation="claude-opus-4",  # operator chooses
#:         ),
#:     )
#:
#: The value of the constant is the human-readable ADR reference; it is
#: NOT load-bearing for dispatch logic. Mirrors ADR-029's pattern for
#: refactor-resistant inline markers.
TIER_LADDER_DEFERRED: Final[str] = (
    "ADR-031: tier-to-model ladder deferred to Epic 09 eval. "
    "v0.1 ships Option A (single env LCM_SUMMARY_MODEL, NULL model_recommendation "
    "in seed). Override per-tier via register_prompt(model_recommendation=...)."
)


def resolve_default_model_from_env() -> str:
    """Resolve the synthesis default model from ``LCM_SUMMARY_MODEL`` at call time.

    Unlike :data:`SYNTHESIS_TIER_DEFAULTS` (which is populated at
    ``dispatch.py`` import time and frozen for the lifetime of the
    process), this function re-reads the environment **at call time**.
    Use it from health-check and ``/lcm status`` paths where the operator
    expects "what would happen if I called dispatch now" rather than
    "what was the env when the plugin loaded."

    Note the two surfaces can disagree if the operator changes
    ``LCM_SUMMARY_MODEL`` after the plugin starts — the dispatch path
    keeps the import-time value (matching TS semantics at
    :file:`lossless-claw/src/synthesis/dispatch.ts:71`), and this
    function returns the live value. A future PR may want to surface
    that drift in ``/lcm health`` output.

    Returns:
        The resolved model identifier. Trimmed of whitespace; empty
        string falls back to :data:`DEFAULT_SUMMARY_MODEL_FALLBACK`.

    Example:
        >>> import os
        >>> os.environ["LCM_SUMMARY_MODEL"] = "claude-sonnet-4"
        >>> resolve_default_model_from_env()
        'claude-sonnet-4'
        >>> os.environ.pop("LCM_SUMMARY_MODEL")  # doctest: +SKIP
        >>> resolve_default_model_from_env()
        'gpt-5.4-mini'
    """

    raw = os.environ.get(LCM_SUMMARY_MODEL_ENV)
    if raw is None:
        return DEFAULT_SUMMARY_MODEL_FALLBACK
    trimmed = raw.strip()
    return trimmed if trimmed else DEFAULT_SUMMARY_MODEL_FALLBACK


def pick_synthesis_model(req: SynthesizeRequest, primary_prompt: PromptRecord) -> str:
    """Resolve the model identifier for a synthesis request — public surface.

    Thin wrapper over the private
    :func:`lossless_hermes.synthesis.dispatch._pick_model`. The
    precedence is preserved verbatim from TS
    (:file:`lossless-claw/src/synthesis/dispatch.ts:755-766`) and from
    the Wave-4 Auditor #5 P1 fix already pinned by
    :class:`tests.synthesis.test_dispatch.TestParityChecklist`:

    1. ``req.force_model=True`` AND ``req.model_override`` set →
       ``req.model_override``.
    2. ``req.force_model=True`` alone → :data:`SYNTHESIS_TIER_DEFAULTS`
       for the request's tier (NOT the prompt's
       ``model_recommendation`` — that was the pre-Wave-4 silent no-op
       bug).
    3. Otherwise: ``primary_prompt.model_recommendation`` if non-None,
       else ``req.model_override`` if set, else
       :data:`SYNTHESIS_TIER_DEFAULTS` for the tier.

    The wrapper exists so that operator tooling, health output, and any
    future external-process consumers can resolve "what model would
    dispatch pick for this request" without importing private dispatch
    internals (which carry a leading underscore by convention).

    Args:
        req: The synthesis request whose tier + override flags drive the
            resolution.
        primary_prompt: The active :class:`PromptRecord` returned by
            :func:`lossless_hermes.synthesis.prompt_registry.get_active_prompt`
            for ``req.memory_type`` + ``req.tier`` + ``"single"`` (or
            ``"best_of_n_judge"`` for yearly).

    Returns:
        The model identifier string that dispatch would pass to the
        injected :class:`LlmCall` callable.

    Example:
        >>> from lossless_hermes.synthesis.types import PromptRecord
        >>> from lossless_hermes.synthesis.dispatch import SynthesizeRequest
        >>> prompt = PromptRecord(
        ...     prompt_id="pr_test", memory_type="episodic-condensed",
        ...     tier_label="daily", pass_kind="single", version=1,
        ...     template="x", model_recommendation="claude-3-5-haiku",
        ...     created_at="2026-05-14T00:00:00Z", active=True,
        ...     bundle_version=1, notes=None,
        ... )
        >>> req = SynthesizeRequest(
        ...     tier="daily", memory_type="episodic-condensed",
        ...     source_text="x", pass_session_id="ps",
        ...     target_summary_id="sum_t",
        ... )
        >>> pick_synthesis_model(req, prompt)
        'claude-3-5-haiku'
    """

    return _pick_model(req, primary_prompt)
