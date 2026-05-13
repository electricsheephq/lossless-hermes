"""Synthesis dispatch — LCM v4.1 §3 / Group D (issue 07-05).

Per-tier model + pass-strategy assignment. Given a synthesis request
(tier label + memory type + input content), this module:

  1. Looks up the active prompt template from :sql:`lcm_prompt_registry`
     via :func:`lossless_hermes.synthesis.prompt_registry.get_active_prompt`
     (issue 07-08 prereq).

  2. Picks the right model + pass strategy for the tier:

     * **daily** → single-pass, mini model
     * **weekly** → single-pass, mid model
     * **monthly** → single-pass + verify-fidelity (hallucination check),
       premium model
     * **yearly** → best-of-N (N=3) + judge, premium model with thinking
     * **custom/filtered** (ad-hoc cache builds) → single-pass, mid model

  3. Calls the injected ``llm_call(args) → LlmCallResult`` for each pass.
     The caller wires this to Hermes's LLM adapter in production; tests
     inject a deterministic async mock.

  4. Records each pass to :sql:`lcm_synthesis_audit` (``started`` →
     ``completed`` / ``failed`` status transition with latency + cost
     telemetry).

  5. Returns the final synthesized text + telemetry. Caller decides
     whether to write to :sql:`summaries.content` (cold rewrites) or to
     :sql:`lcm_synthesis_cache` (ad-hoc / filtered / yearly).

### Why this module is OUTSIDE the existing ``summarize.py`` flow

The existing per-leaf summarizer is geared toward inline compaction
(called by the gateway compactor). v4.1 synthesis is a worker-side
cold rewrite — different tier-aware model selection, different
verification logic, different cache surface. Keeping them separate
prevents regression in the hot path.

### Why no critique-revise multi-pass

Literature consensus is that critique-revise underperforms single-pass
for summarization (architecture-v4.1 §3 + §11). We use:

* Single-pass for daily/weekly (just summarize)
* Single + verify-fidelity for monthly (summarize, then ask a separate
  model "does this contain claims not in source?")
* Best-of-N + judge for yearly (run summarize 3× independently, then a
  judge prompt picks the best)

### LlmCall Protocol

This module defines the canonical :class:`LlmCall` Protocol consumed by
the dispatcher. The entity extractor (issue 07-03) defines an
equivalent :class:`LlmCompleteFn` Protocol with the same shape (dict-
based args) for the inline-extraction worker path. Both are
deliberately compatible so a single Hermes-side adapter can satisfy
both.

### Source pin

* TS canonical: ``lossless-claw/src/synthesis/dispatch.ts`` (commit
  ``1f07fbd`` on branch ``pr-613``, 817 LOC).
* Spec: ``epics/07-entity-synthesis/07-05-synthesis-dispatch.md``.
* ADR-029: ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment
  format.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Literal, Protocol

from lossless_hermes.synthesis.prompt_registry import get_active_prompt
from lossless_hermes.synthesis.types import MemoryType, PassKind, PromptRecord

__all__ = [
    "DEFAULT_MODEL_BY_TIER",
    "HARD_CAP_BEST_OF_N",
    "PASS_STRATEGY_BY_TIER",
    "BestOfNDetail",
    "LlmCall",
    "LlmCallArgs",
    "LlmCallResult",
    "SynthesisDispatchError",
    "SynthesisDispatcher",
    "SynthesizeRequest",
    "SynthesizeResult",
    "TierLabel",
    "dispatch_synthesis",
]


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


TierLabel = Literal["daily", "weekly", "monthly", "yearly", "custom", "filtered"]
"""Per-tier label for synthesis dispatch.

Mirrors the TS union at ``dispatch.ts:54-60``.
"""


# ---------------------------------------------------------------------------
# Tier defaults (model + pass strategy)
# ---------------------------------------------------------------------------


def _resolve_default_model() -> str:
    """Resolve the default model from ``LCM_SUMMARY_MODEL`` env at call time.

    Read at module-import so the per-tier defaults table below is a
    plain dict (the TS source has the same shape — env evaluated at
    module load). Tests that need a different default override the
    relevant table entry directly.

    Matches the TS expression
    ``process.env.LCM_SUMMARY_MODEL?.trim() || "gpt-5.4-mini"`` at
    ``dispatch.ts:71``.
    """

    raw = os.environ.get("LCM_SUMMARY_MODEL")
    if raw is None:
        return "gpt-5.4-mini"
    trimmed = raw.strip()
    return trimmed if trimmed else "gpt-5.4-mini"


_LCM_DEFAULT_MODEL = _resolve_default_model()


#: Per-tier model recommendation. All tiers default to the same model
#: — operators pick a single default via ``LCM_SUMMARY_MODEL`` env
#: (matching the existing leaf-summarizer convention in
#: :mod:`lossless_hermes.summarize`). Tier-specific tuning is done by
#: setting :attr:`PromptRecord.model_recommendation` per-prompt in
#: :sql:`lcm_prompt_registry`, **NOT** by hardcoded tier defaults.
#: Falls back to ``"gpt-5.4-mini"`` if env unset.
#:
#: TS source: ``dispatch.ts:72-79``.
DEFAULT_MODEL_BY_TIER: dict[TierLabel, str] = {
    "daily": _LCM_DEFAULT_MODEL,
    "weekly": _LCM_DEFAULT_MODEL,
    "monthly": _LCM_DEFAULT_MODEL,
    "yearly": _LCM_DEFAULT_MODEL,
    "custom": _LCM_DEFAULT_MODEL,
    "filtered": _LCM_DEFAULT_MODEL,
}


#: Pass strategy per tier. The synthesis flow runs the listed passes in
#: order. Yearly's ``"best_of_n_judge"`` is expanded inside dispatch to
#: N=3 single-pass + 1 judge.
#:
#: TS source: ``dispatch.ts:83-90``.
PASS_STRATEGY_BY_TIER: dict[TierLabel, list[PassKind]] = {
    "daily": ["single"],
    "weekly": ["single"],
    "monthly": ["single", "verify_fidelity"],
    "yearly": ["best_of_n_judge"],
    "custom": ["single"],
    "filtered": ["single"],
}


#: Hard cap on the best-of-N candidate count for yearly tier. A caller
#: passing ``best_of_n=10`` would otherwise spend ~$100+ per yearly
#: synthesis call (10× single-pass premium model). The clamp is
#: surfaced via :attr:`BestOfNDetail.requested` + :attr:`BestOfNDetail.capped`
#: so callers can see + audit the decision.
#:
#: TS source: ``dispatch.ts:256``.
HARD_CAP_BEST_OF_N: int = 5


# ---------------------------------------------------------------------------
# LlmCall Protocol + supporting dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LlmCallArgs:
    """Args passed to the injected :class:`LlmCall` callable.

    Mirrors the TS ``LlmCallArgs`` interface at ``dispatch.ts:92-100``.
    Frozen so a misbehaving adapter can't mutate caller-controlled state.
    """

    model: str
    """The model identifier to dispatch (e.g. ``"gpt-5.4-mini"``)."""

    prompt: str
    """The fully-rendered prompt text (template + substitutions)."""

    pass_kind: PassKind
    """Which pass this call is part of — recorded on the audit row."""

    max_output_tokens: int | None = None
    """Optional max output tokens (caller may have a budget)."""


@dataclass(frozen=True, slots=True)
class LlmCallResult:
    """Result returned by the injected :class:`LlmCall` callable.

    Mirrors the TS ``LlmCallResult`` interface at ``dispatch.ts:102-111``.
    Frozen so audit recording can't drift from the value the adapter
    reported.
    """

    output: str
    """Generated text."""

    latency_ms: float
    """Latency observed by the caller (used for audit)."""

    cost_cents: int | None = None
    """USD cents (rounded), if known. Used for audit + telemetry."""

    actual_model: str | None = None
    """Override the model name recorded (e.g. fallback chain triggered)."""


class LlmCall(Protocol):
    """The injected LLM-call callable (canonical synthesis-side Protocol).

    The dispatch module stays vendor-agnostic — the Hermes-side adapter
    (which wraps ``anthropic.AsyncAnthropic`` or equivalent) lives in
    :mod:`lossless_hermes.synthesis.llm_adapter` (forthcoming). The entity
    extractor (issue 07-03 :mod:`lossless_hermes.extraction.extractor`)
    defines an equivalent :class:`LlmCompleteFn` Protocol with the same
    shape so a single adapter satisfies both.

    The signature is async because the TS canonical source returns
    ``Promise<LlmCallResult>``. Production wiring satisfies this via
    ``async def call(args): ...``; tests inject deterministic mocks.

    Mirrors the TS ``LlmCall`` type at ``dispatch.ts:114``.
    """

    async def __call__(self, args: LlmCallArgs, /) -> LlmCallResult: ...


# ---------------------------------------------------------------------------
# Request / Result / Error
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SynthesizeRequest:
    """One synthesis request — what to summarize and how.

    Mirrors the TS ``SynthesizeRequest`` interface at
    ``dispatch.ts:116-154``. Frozen so the request value can't drift
    between dispatch and audit recording.

    Either :attr:`target_summary_id` (rewriting an existing
    :sql:`summaries` row) OR :attr:`target_cache_id` (writing to
    :sql:`lcm_synthesis_cache`) must be set — the audit table's CHECK
    constraint requires one of them. Passing neither raises
    :exc:`SynthesisDispatchError` (``kind="missing_target"``) before
    the LLM is called.

    LCM Group D adversarial Gap 1 fix: previous docstring claimed
    dry-run was supported via skipping the audit row; the implementation
    always inserted, so dry-run requests crashed with a raw SQLite error
    before any LLM call. The :attr:`target_summary_id` /
    :attr:`target_cache_id` validation here catches the missing case
    early with a typed error.
    Original: ``lossless-claw/src/synthesis/dispatch.ts:116-154``.
    """

    tier: TierLabel
    """The tier label (``"daily"``, ``"weekly"`` etc.). Determines model + pass strategy."""

    memory_type: MemoryType
    """Memory-type discriminator. One of the six values in :data:`MemoryType`."""

    source_text: str
    """Input content to synthesize (e.g. concat of leaf contents)."""

    pass_session_id: str
    """Pass-session ID — groups multiple audit rows for one synthesis pass.

    All candidates + judge in a yearly best-of-N attempt share the
    same ``pass_session_id`` (Group D adversarial Gap 2).
    """

    target_summary_id: str | None = None
    """Target summaries row (cold rewrites). Set XOR ``target_cache_id``."""

    target_cache_id: str | None = None
    """Target cache row (ad-hoc / filtered / yearly). Set XOR ``target_summary_id``."""

    model_override: str | None = None
    """Override default model for this tier (A/B test). See :func:`_pick_model`."""

    force_model: bool = False
    """Force a specific model regardless of prompt's model_recommendation.

    Precedence (LCM Wave-4 Auditor #5 P1 fix, see :func:`_pick_model`):

    * ``force_model=True`` and ``model_override`` set → ``model_override``
    * ``force_model=True`` alone → :data:`DEFAULT_MODEL_BY_TIER` for tier
    * otherwise → ``prompt.model_recommendation`` or ``model_override``
      or :data:`DEFAULT_MODEL_BY_TIER` for tier
    """

    best_of_n: int | None = None
    """Best-of-N count for yearly tier. Default 3; hard-capped at 5."""


@dataclass(frozen=True, slots=True)
class BestOfNDetail:
    """Best-of-N execution detail (yearly tier).

    Surfaces the actual number of candidates that ran, which the judge
    picked, and (for caller-side audit) whether the request was clamped
    against :data:`HARD_CAP_BEST_OF_N`.

    Mirrors the TS ``bestOfN`` inline object at ``dispatch.ts:170-183``.

    LCM Wave-5 P2 (2026-01-12): :attr:`requested` and :attr:`capped`
    surface the clamp so callers can see + audit cost decisions.
    Original: ``lossless-claw/src/synthesis/dispatch.ts:177-182``.
    """

    n: int
    """Number of candidates that actually ran (may be < requested if some failed)."""

    selected_index: int
    """Index of the candidate the judge picked. ``0`` for single-survivor short-circuit."""

    candidates: list[str]
    """All candidate outputs, in order. Length == ``n``."""

    requested: int | None = None
    """Caller-requested ``best_of_n``. May exceed :data:`HARD_CAP_BEST_OF_N`."""

    capped: bool = False
    """``True`` if ``requested > HARD_CAP_BEST_OF_N`` and was clamped."""


@dataclass(frozen=True, slots=True)
class SynthesizeResult:
    """One synthesis result — final text + telemetry.

    Mirrors the TS ``SynthesizeResult`` interface at
    ``dispatch.ts:156-184``. Frozen so the result is the same object
    everywhere it's consumed.

    LCM Wave-8 (2026-03-08) CRITICAL: the 1-survivor short-circuit
    previously returned a malformed result missing required fields
    (:attr:`primary_prompt_id`, :attr:`audit_ids`, :attr:`total_latency_ms`,
    :attr:`total_cost_cents`) — a TYPE CONTRACT VIOLATION that would
    crash callers reading those fields. The dataclass shape here forces
    all of them to be populated.
    Original: ``lossless-claw/src/synthesis/dispatch.ts:632-647``.
    """

    output: str
    """Final synthesized text."""

    primary_prompt_id: str
    """Active prompt used for the primary single-pass."""

    audit_ids: list[str]
    """Audit rows written. Caller may use audit IDs to back-reference."""

    total_latency_ms: float
    """Total latency across all passes."""

    total_cost_cents: int
    """Total USD cents across all passes (sum of per-call costs)."""

    hallucination_flagged: bool | None = None
    """``True`` if verify-fidelity pass flagged hallucinations (monthly tier).

    ``None`` for tiers that don't run verify_fidelity, or if no
    verify_fidelity prompt is registered (skipped silently).
    """

    best_of_n: BestOfNDetail | None = None
    """Best-of-N detail (yearly tier only). ``None`` for other tiers."""


class SynthesisDispatchError(RuntimeError):
    """Typed dispatch failure.

    Mirrors the TS ``SynthesisDispatchError`` class at
    ``dispatch.ts:186-199``. The ``kind`` discriminator is also
    available as :attr:`args[0]` so callers can ``match`` it without
    importing :class:`sqlite3`-style sentinels.

    Kinds:

    * ``"missing_prompt"`` — no active prompt registered for the triple
    * ``"missing_target"`` — neither :attr:`SynthesizeRequest.target_summary_id`
      nor :attr:`SynthesizeRequest.target_cache_id` is set
    * ``"llm_failure"`` — the injected ``llm_call`` raised
    * ``"judge_failure"`` — yearly judge output didn't parse to a valid index
    * ``"audit_insert_failure"`` — the ``'started'`` audit row insert
      failed (FK / CHECK violation); the LLM was NOT called
    """

    kind: Literal[
        "missing_prompt",
        "missing_target",
        "llm_failure",
        "judge_failure",
        "audit_insert_failure",
    ]
    """The error kind discriminator."""

    def __init__(
        self,
        kind: Literal[
            "missing_prompt",
            "missing_target",
            "llm_failure",
            "judge_failure",
            "audit_insert_failure",
        ],
        message: str,
    ) -> None:
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------------------
# Dispatcher class — load-bearing async machinery
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PassAuditCtx:
    """Internal: context for one ``runPassWithAudit`` call.

    Not part of the public API — only used by :class:`SynthesisDispatcher`
    to thread the audit-row fields through to the writer. Frozen would be
    cleaner but the ``slots`` makes the per-call allocation cheap.

    Mirrors the TS ``PassAuditCtx`` interface at ``dispatch.ts:372-379``.
    """

    pass_session_id: str
    prompt_id: str
    target_summary_id: str | None
    target_cache_id: str | None
    pass_input_for_audit: str


@dataclass(slots=True)
class _PassResult:
    """Internal: result of one ``runPassWithAudit`` call.

    Not part of the public API. Mirrors the TS ``PassResult`` interface
    at ``dispatch.ts:381-387``.
    """

    audit_id: str
    output: str
    latency_ms: float
    cost_cents: int | None
    actual_model: str


class SynthesisDispatcher:
    """Tier-aware synthesis dispatcher.

    Construct with a DB connection + an :class:`LlmCall` callable; call
    :meth:`synthesize` for each request. The dispatcher is stateless
    beyond its two constructor args — no module-level singletons (per
    AC).

    Args:
        db: Open :class:`sqlite3.Connection`. The dispatcher uses it for
            prompt lookups (via :func:`prompt_registry.get_active_prompt`)
            and audit-row writes. Caller controls transaction state.
        llm_call: The injected LLM-call callable matching :class:`LlmCall`.
            Production wires this to a Hermes-side
            ``anthropic.AsyncAnthropic`` wrapper; tests inject a
            deterministic async mock.

    Example::

        dispatcher = SynthesisDispatcher(db, llm_call)
        result = await dispatcher.synthesize(
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="...",
                pass_session_id="ps_abc123",
                target_summary_id="sum_target",
            )
        )

    Mirrors the TS ``dispatchSynthesis`` async function at
    ``dispatch.ts:211-368``. The Python port wraps the function as a
    class so the (db, llm_call) pair is bound once instead of being
    threaded through every call (matches the AC "no module-level
    singletons").
    """

    def __init__(self, db: sqlite3.Connection, llm_call: LlmCall) -> None:
        self._db = db
        self._llm_call = llm_call

    # ----- public entrypoint ------------------------------------------------

    async def synthesize(self, req: SynthesizeRequest) -> SynthesizeResult:
        """Dispatch a synthesis request. See class docstring for the pipeline.

        Raises:
            SynthesisDispatchError: ``kind`` indicates the failure mode.
            See :class:`SynthesisDispatchError` for the kinds.

        Returns:
            :class:`SynthesizeResult` with primary output + telemetry.
            For yearly tier, :attr:`SynthesizeResult.best_of_n` is
            populated; for monthly tier with a verify_fidelity prompt
            registered, :attr:`SynthesizeResult.hallucination_flagged`
            is populated.
        """

        # LCM Wave-9 Group D adversarial Gap 1 (2026-03-08): validate target
        # up-front. The CHECK constraint on lcm_synthesis_audit would catch
        # this later anyway, but we throw a clear typed error before
        # touching the LLM (the audit insert sits AFTER the typed-error
        # path so dry-run requests can't crash with a raw SQLite error).
        # Original: lossless-claw/src/synthesis/dispatch.ts:218-225.
        if req.target_summary_id is None and req.target_cache_id is None:
            raise SynthesisDispatchError(
                "missing_target",
                "[synthesis.dispatch] either target_summary_id or target_cache_id "
                "is required (lcm_synthesis_audit CHECK constraint requires one of "
                "them set)",
            )

        # 1. Look up active prompt for the primary pass.
        tier = req.tier
        pass_kinds = PASS_STRATEGY_BY_TIER[tier]
        primary_pass_kind: PassKind = "best_of_n_judge" if tier == "yearly" else "single"
        # The primary lookup is always against passKind="single" — the
        # yearly tier's primary candidates ARE single-pass renders; the
        # best_of_n_judge prompt is fetched separately inside the yearly
        # branch.
        lookup_pass_kind: PassKind = (
            "single" if primary_pass_kind == "best_of_n_judge" else primary_pass_kind
        )
        primary_prompt = get_active_prompt(
            self._db,
            memory_type=req.memory_type,
            tier_label=tier,
            pass_kind=lookup_pass_kind,
        )
        if primary_prompt is None:
            raise SynthesisDispatchError(
                "missing_prompt",
                f"[synthesis.dispatch] no active prompt for (memory_type={req.memory_type}, "
                f"tier={tier}, pass_kind=single)",
            )

        # 2. Pick model (Wave-4 Auditor #5 P1 precedence chain).
        model = _pick_model(req, primary_prompt)

        # 3. Branch on tier.
        if tier == "yearly":
            # LCM Wave-4 Auditor #5 P1 + Wave-5 P2 (2026-01-12): hard-cap
            # best_of_n at 5 to prevent unbounded cost (caller passing
            # best_of_n=100 would spend ~$100+ per yearly synthesis call).
            # Surface clamp via BestOfNDetail.requested + .capped so
            # callers can see + audit it.
            # Original: lossless-claw/src/synthesis/dispatch.ts:251-269.
            requested_best_of_n = req.best_of_n if req.best_of_n is not None else 3
            capped_best_of_n = max(1, min(HARD_CAP_BEST_OF_N, requested_best_of_n))
            result = await self._run_best_of_n_yearly(req, primary_prompt, model, capped_best_of_n)
            if result.best_of_n is not None:
                # Re-emit with requested / capped flags filled in.
                return SynthesizeResult(
                    output=result.output,
                    primary_prompt_id=result.primary_prompt_id,
                    audit_ids=result.audit_ids,
                    total_latency_ms=result.total_latency_ms,
                    total_cost_cents=result.total_cost_cents,
                    hallucination_flagged=result.hallucination_flagged,
                    best_of_n=BestOfNDetail(
                        n=result.best_of_n.n,
                        selected_index=result.best_of_n.selected_index,
                        candidates=result.best_of_n.candidates,
                        requested=requested_best_of_n,
                        capped=requested_best_of_n > HARD_CAP_BEST_OF_N,
                    ),
                )
            return result

        # 4. Standard single-pass (daily/weekly/custom/filtered): one LLM call.
        single_pass_prompt = _render_prompt(primary_prompt.template, req)
        single_result = await self._run_pass_with_audit(
            LlmCallArgs(
                model=model,
                prompt=single_pass_prompt,
                pass_kind="single",
                max_output_tokens=None,
            ),
            _PassAuditCtx(
                pass_session_id=req.pass_session_id,
                prompt_id=primary_prompt.prompt_id,
                target_summary_id=req.target_summary_id,
                target_cache_id=req.target_cache_id,
                pass_input_for_audit=req.source_text,
            ),
        )
        audit_ids: list[str] = [single_result.audit_id]
        total_latency_ms = single_result.latency_ms
        total_cost_cents = single_result.cost_cents or 0

        hallucination_flagged: bool | None = None

        # 5. Optional verify-fidelity pass (monthly tier).
        if "verify_fidelity" in pass_kinds:
            verify_prompt = get_active_prompt(
                self._db,
                memory_type=req.memory_type,
                tier_label=tier,
                pass_kind="verify_fidelity",
            )
            if verify_prompt is not None:
                rendered_verify = _render_verify_prompt(
                    verify_prompt.template,
                    source_text=req.source_text,
                    candidate_summary=single_result.output,
                )
                verify_result = await self._run_pass_with_audit(
                    LlmCallArgs(
                        model=model,
                        prompt=rendered_verify,
                        pass_kind="verify_fidelity",
                        max_output_tokens=100,
                    ),
                    _PassAuditCtx(
                        pass_session_id=req.pass_session_id,
                        prompt_id=verify_prompt.prompt_id,
                        target_summary_id=req.target_summary_id,
                        target_cache_id=req.target_cache_id,
                        pass_input_for_audit=single_result.output,
                    ),
                )
                audit_ids.append(verify_result.audit_id)
                total_latency_ms += verify_result.latency_ms
                total_cost_cents += verify_result.cost_cents or 0
                hallucination_flagged = _parse_verify_output(verify_result.output)
            # If no verify prompt registered, skip silently — caller can
            # decide to enforce its presence via /lcm health.

        return SynthesizeResult(
            output=single_result.output,
            primary_prompt_id=primary_prompt.prompt_id,
            audit_ids=audit_ids,
            total_latency_ms=total_latency_ms,
            total_cost_cents=total_cost_cents,
            hallucination_flagged=hallucination_flagged,
            best_of_n=None,
        )

    # ----- internals --------------------------------------------------------

    async def _run_pass_with_audit(
        self,
        llm_args: LlmCallArgs,
        audit: _PassAuditCtx,
    ) -> _PassResult:
        """One LLM call with the surrounding ``started`` → ``completed`` /
        ``failed`` audit-row lifecycle.

        Three phases:

        1. Insert ``'started'`` audit row (try/except wraps it — see
           Wave-9 Group D Gap 4).
        2. Call the injected ``llm_call``. On failure, update audit
           row to ``'failed'`` + raise typed :exc:`SynthesisDispatchError`.
        3. Update audit row to ``'completed'`` with the output +
           telemetry.
        """

        audit_id = f"audit_{audit.pass_session_id}_{audit.prompt_id[-6:]}_{_random_suffix()}"
        # LCM Wave-9 Group D adversarial Gap 4 (2026-03-08): wrap insert
        # in try/except so FK violations (bad target_summary_id) and
        # CHECK violations surface as typed SynthesisDispatchError
        # (audit_insert_failure) instead of raw SQLite errors. Forensic
        # record still attempted; if THAT fails, the typed error tells
        # the caller the LLM was never called.
        # Original: lossless-claw/src/synthesis/dispatch.ts:401-420.
        try:
            _insert_audit_row(
                self._db,
                audit_id=audit_id,
                pass_session_id=audit.pass_session_id,
                target_summary_id=audit.target_summary_id,
                target_cache_id=audit.target_cache_id,
                prompt_id=audit.prompt_id,
                pass_kind=llm_args.pass_kind,
                pass_input_truncated=_truncate_for_audit(audit.pass_input_for_audit),
                status="started",
                model_used=llm_args.model,
            )
        except sqlite3.DatabaseError as exc:
            raise SynthesisDispatchError(
                "audit_insert_failure",
                f"[synthesis.dispatch] failed to insert 'started' audit row "
                f"(LLM not called): {exc}",
            ) from exc

        try:
            result = await self._llm_call(llm_args)
        except Exception as exc:  # noqa: BLE001 — vendor-specific exceptions vary
            _update_audit_row(
                self._db,
                audit_id,
                status="failed",
                last_error=str(exc),
            )
            raise SynthesisDispatchError(
                "llm_failure",
                f"[synthesis.dispatch] LLM call failed for pass {llm_args.pass_kind}: {exc}",
            ) from exc

        # LCM Wave-9 (2026-03-08): pass_output truncated to 8000 chars
        # with "…(truncated)" marker. Full outputs are not retained.
        # Original: lossless-claw/src/synthesis/dispatch.ts:440.
        _update_audit_row(
            self._db,
            audit_id,
            status="completed",
            pass_output=_truncate_for_audit(result.output),
            model_used=result.actual_model or llm_args.model,
            latency_ms=round(result.latency_ms),
            cost_cents=(round(result.cost_cents) if result.cost_cents is not None else None),
        )

        return _PassResult(
            audit_id=audit_id,
            output=result.output,
            latency_ms=result.latency_ms,
            cost_cents=result.cost_cents,
            actual_model=result.actual_model or llm_args.model,
        )

    async def _run_best_of_n_yearly(
        self,
        req: SynthesizeRequest,
        primary_prompt: PromptRecord,
        model: str,
        best_of_n: int,
    ) -> SynthesizeResult:
        """Yearly tier — N candidates in parallel + 1 judge call.

        Three phases:

        1. Run N single-pass candidates in parallel via
           :func:`asyncio.gather` with ``return_exceptions=True``
           (Wave-7 P1.1 fix — failed candidate doesn't poison successful
           siblings).
        2. If only one survivor → skip judge entirely (Wave-7 P1.2 +
           Wave-8 P0 CRITICAL — judge over N=1 is a foot-gun; the
           short-circuit returns a complete :class:`SynthesizeResult`
           with all required fields).
        3. Look up the judge prompt; raise ``missing_prompt`` if absent.
           Render judge prompt with all surviving candidates. Run it,
           parse the index, return :class:`SynthesizeResult`.

        All candidates + judge share ONE ``pass_session_id`` (Wave-9
        Group D Gap 2). Per-candidate disambiguation lives in
        ``pass_input_truncated`` + the sequential ``ran_at`` timestamps.
        """

        rendered_single = _render_prompt(primary_prompt.template, req)

        # Run N candidates in parallel.
        async def _one_candidate() -> _PassResult:
            return await self._run_pass_with_audit(
                LlmCallArgs(
                    model=model,
                    prompt=rendered_single,
                    pass_kind="single",
                    max_output_tokens=None,
                ),
                _PassAuditCtx(
                    # LCM Wave-9 Group D adversarial Gap 2 (2026-03-08):
                    # ALL passes in this best-of-N attempt share the
                    # same pass_session_id so operators can
                    # SELECT WHERE pass_session_id = X to retrieve the
                    # full attempt's audit trail. Previously each
                    # candidate had a distinct `_cand{i}` suffix,
                    # splattering rows across N+1 distinct sessions and
                    # breaking the orphan-GC invariant.
                    # Original: lossless-claw/src/synthesis/dispatch.ts:565-579.
                    pass_session_id=req.pass_session_id,
                    prompt_id=primary_prompt.prompt_id,
                    target_summary_id=req.target_summary_id,
                    target_cache_id=req.target_cache_id,
                    pass_input_for_audit=req.source_text,
                ),
            )

        # LCM Wave-7 Auditor #5 P1.1 (2026-02-14): use
        # asyncio.gather(*, return_exceptions=True) — the Python
        # equivalent of Promise.allSettled — so one failed candidate
        # doesn't discard the work of successful peers. Cost: yearly
        # already paid for the successful runs; previous behavior wasted
        # that money. New behavior: collect successes, raise only if ALL
        # fail; judge picks among survivors.
        # Original: lossless-claw/src/synthesis/dispatch.ts:581-609.
        settled: list[_PassResult | BaseException] = await asyncio.gather(
            *(_one_candidate() for _ in range(best_of_n)),
            return_exceptions=True,
        )
        candidate_results: list[_PassResult] = []
        candidate_failures: list[str] = []
        for s in settled:
            if isinstance(s, BaseException):
                candidate_failures.append(str(s))
            else:
                candidate_results.append(s)
        if not candidate_results:
            # ALL candidates failed — surface all failures.
            raise SynthesisDispatchError(
                "llm_failure",
                f"[synthesis.dispatch] all {best_of_n} best-of-N candidates failed: "
                f"{' | '.join(candidate_failures)[:600]}",
            )

        audit_ids: list[str] = [c.audit_id for c in candidate_results]

        # LCM Wave-7 Auditor #5 P1.2 (2026-02-14) + Wave-8 Auditor #2-5
        # P1 CRITICAL (2026-03-08): skip judge entirely when only ONE
        # candidate survived (either best_of_n=1 caller OR N-1
        # candidates failed). Judge over a single candidate is a
        # foot-gun (judge expected 0..N-1 but only "0" would be valid;
        # many models emit 1-indexed and crash judge_failure). The
        # short-circuit returns a COMPLETE SynthesizeResult with all
        # required fields populated from the single survivor — Wave-8
        # caught the previous TYPE CONTRACT VIOLATION where the
        # short-circuit was missing primary_prompt_id / audit_ids /
        # total_latency_ms / total_cost_cents.
        # Original: lossless-claw/src/synthesis/dispatch.ts:622-647.
        if len(candidate_results) == 1:
            sole = candidate_results[0]
            return SynthesizeResult(
                output=sole.output,
                primary_prompt_id=primary_prompt.prompt_id,
                audit_ids=audit_ids,
                total_latency_ms=sole.latency_ms,
                total_cost_cents=sole.cost_cents or 0,
                hallucination_flagged=None,
                best_of_n=BestOfNDetail(
                    n=1,
                    selected_index=0,
                    candidates=[sole.output],
                ),
            )

        # Look up the judge prompt.
        judge_prompt = get_active_prompt(
            self._db,
            memory_type=req.memory_type,
            tier_label=req.tier,
            pass_kind="best_of_n_judge",
        )
        if judge_prompt is None:
            raise SynthesisDispatchError(
                "missing_prompt",
                f"[synthesis.dispatch] yearly tier requires best_of_n_judge prompt "
                f"for memory_type={req.memory_type}, tier={req.tier}",
            )

        rendered_judge = _render_judge_prompt(
            judge_prompt.template,
            source_text=req.source_text,
            candidates=[c.output for c in candidate_results],
        )
        judge_result = await self._run_pass_with_audit(
            LlmCallArgs(
                model=model,
                prompt=rendered_judge,
                pass_kind="best_of_n_judge",
                max_output_tokens=50,
            ),
            _PassAuditCtx(
                # Wave-9 Group D Gap 2: judge call shares the same
                # pass_session_id as the candidate calls.
                pass_session_id=req.pass_session_id,
                prompt_id=judge_prompt.prompt_id,
                target_summary_id=req.target_summary_id,
                target_cache_id=req.target_cache_id,
                pass_input_for_audit="\n---\n".join(c.output for c in candidate_results),
            ),
        )
        audit_ids.append(judge_result.audit_id)

        # LCM Wave-8 Auditor #2-5 D-P1 (2026-03-08): judge sees only
        # SURVIVORS (post asyncio.gather), not the originally-requested
        # N. Pass len(candidate_results) so parse_judge_output's range
        # check matches the actual choice space. Previously passed
        # best_of_n — a judge picking index 2 when only 2 survivors
        # existed would have indexed out of bounds.
        # Original: lossless-claw/src/synthesis/dispatch.ts:692-698.
        selected_index = _parse_judge_output(judge_result.output, len(candidate_results))

        total_latency_ms = sum(c.latency_ms for c in candidate_results) + judge_result.latency_ms
        total_cost_cents = sum(c.cost_cents or 0 for c in candidate_results) + (
            judge_result.cost_cents or 0
        )

        return SynthesizeResult(
            output=candidate_results[selected_index].output,
            primary_prompt_id=primary_prompt.prompt_id,
            audit_ids=audit_ids,
            total_latency_ms=total_latency_ms,
            total_cost_cents=total_cost_cents,
            hallucination_flagged=None,
            best_of_n=BestOfNDetail(
                # Reflect actual N executed (may be < best_of_n if some failed).
                n=len(candidate_results),
                selected_index=selected_index,
                candidates=[c.output for c in candidate_results],
            ),
        )


# ---------------------------------------------------------------------------
# Module-level convenience wrapper (matches TS function-style export)
# ---------------------------------------------------------------------------


async def dispatch_synthesis(
    db: sqlite3.Connection,
    llm_call: LlmCall,
    req: SynthesizeRequest,
) -> SynthesizeResult:
    """Module-level convenience wrapper around :class:`SynthesisDispatcher`.

    Constructs a dispatcher inline and calls
    :meth:`SynthesisDispatcher.synthesize`. Prefer
    :class:`SynthesisDispatcher` directly in production (the dispatcher
    is cheap to construct, but binding the ``(db, llm_call)`` pair once
    is clearer than re-passing them per call).

    Mirrors the TS ``dispatchSynthesis`` async function at
    ``dispatch.ts:211-368``.
    """

    return await SynthesisDispatcher(db, llm_call).synthesize(req)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def _pick_model(req: SynthesizeRequest, primary_prompt: PromptRecord) -> str:
    """Resolve the model identifier for this request.

    Precedence (LCM Wave-4 Auditor #5 P1, see Wave-N comment below):

    * ``force_model=True`` and ``model_override`` set → ``model_override``
    * ``force_model=True`` alone → :data:`DEFAULT_MODEL_BY_TIER` for tier
      (NOT ``prompt.model_recommendation`` — that's the previous bug)
    * otherwise:
      * ``prompt.model_recommendation`` if non-None
      * else ``model_override`` if set
      * else :data:`DEFAULT_MODEL_BY_TIER` for tier

    LCM Wave-4 Auditor #5 P1 (2026-01-12): ``force_model`` without
    ``model_override`` was silently no-op (fell through to prompt's
    ``model_recommendation``). Now: ``force_model`` without
    ``model_override`` forces the tier default, so callers can
    guarantee "default model regardless of prompt recommendation."
    Original: ``lossless-claw/src/synthesis/dispatch.ts:755-766``.
    """

    if req.force_model:
        return req.model_override or DEFAULT_MODEL_BY_TIER[req.tier]
    if primary_prompt.model_recommendation:
        return primary_prompt.model_recommendation
    return req.model_override or DEFAULT_MODEL_BY_TIER[req.tier]


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


# Pre-compiled regex patterns (compiled at import; per-render path is hot).
_SUB_SOURCE_TEXT = re.compile(r"\{\{\s*source_text\s*\}\}")
_SUB_SOURCE_LEAVES = re.compile(r"\{\{\s*source_leaves\s*\}\}")
_SUB_CANDIDATE_SUMMARY = re.compile(r"\{\{\s*candidate_summary\s*\}\}")
_SUB_DRAFT = re.compile(r"\{\{\s*draft\s*\}\}")
_SUB_CANDIDATES = re.compile(r"\{\{\s*candidates\s*\}\}")
_SUB_TIER = re.compile(r"\{\{\s*tier\s*\}\}")
_SUB_MEMORY_TYPE = re.compile(r"\{\{\s*memory_type\s*\}\}")


def _render_prompt(template: str, req: SynthesizeRequest) -> str:
    """Render the primary single-pass prompt template.

    Substitutes ``{{source_text}}``, ``{{tier}}``, and ``{{memory_type}}``
    placeholders. Other tokens are left verbatim (the caller can
    pre-render if needed).

    Mirrors the TS ``renderPrompt`` at ``dispatch.ts:768-776``.
    """

    out = _SUB_SOURCE_TEXT.sub(req.source_text, template)
    out = _SUB_TIER.sub(req.tier, out)
    out = _SUB_MEMORY_TYPE.sub(req.memory_type, out)
    return out


def _render_verify_prompt(template: str, *, source_text: str, candidate_summary: str) -> str:
    """Render the verify_fidelity prompt template.

    Substitutes BOTH the dispatch-canonical placeholders AND the
    spec-named placeholders so either template form works:

    * ``{{source_text}}`` AND ``{{source_leaves}}`` → ``source_text``
    * ``{{candidate_summary}}`` AND ``{{draft}}`` → ``candidate_summary``

    LCM Final.review.3 Loop 4 Bug 4.2 (2026-01-12): the seeded §12
    verify_fidelity prompt uses ``{{draft}}`` and ``{{source_leaves}}``
    placeholders (matching architecture-v4.1.md §12 / Appendix A
    literally). The renderer previously only handled
    ``{{candidate_summary}}`` and ``{{source_text}}``, so the seeded
    template was sent verbatim to the LLM with placeholders
    un-substituted — making the entire monthly verify pass meaningless.
    We now substitute BOTH the spec-named placeholders AND the
    dispatch-canonical ones, so either prompt template works.
    Original: ``lossless-claw/src/synthesis/dispatch.ts:778-795``.
    """

    out = _SUB_SOURCE_TEXT.sub(source_text, template)
    out = _SUB_SOURCE_LEAVES.sub(source_text, out)
    out = _SUB_CANDIDATE_SUMMARY.sub(candidate_summary, out)
    out = _SUB_DRAFT.sub(candidate_summary, out)
    return out


def _render_judge_prompt(template: str, *, source_text: str, candidates: list[str]) -> str:
    """Render the best_of_n_judge prompt template.

    Substitutes ``{{source_text}}`` and ``{{candidates}}``. The
    candidates are joined with ``### Candidate <i>`` headers so the
    judge can refer to them by index.

    Mirrors the TS ``renderJudgePrompt`` at ``dispatch.ts:797-807``.
    """

    candidates_list = "\n\n".join(f"### Candidate {i}\n\n{c}" for i, c in enumerate(candidates))
    out = _SUB_SOURCE_TEXT.sub(source_text, template)
    out = _SUB_CANDIDATES.sub(candidates_list, out)
    return out


# ---------------------------------------------------------------------------
# Verify-output parsing
# ---------------------------------------------------------------------------


# Pre-compiled regex patterns for verify-fidelity output parsing.
# Both checked together: positive OK marker AND absence of negative markers.
_VERIFY_OK_RE = re.compile(r"(?:^|\n)\s*OK\b", re.IGNORECASE)
_VERIFY_NEGATIVE_RE = re.compile(r"(?:^|\n)\s*(?:UNSUPPORTED|HALLUCINATION)\s*:", re.IGNORECASE)


def _parse_verify_output(output: str) -> bool:
    """Parse the verify_fidelity LLM output into ``hallucination_flagged``.

    Returns ``True`` if the verify pass FLAGGED hallucinations
    (UNSUPPORTED: / HALLUCINATION: marker present, OR no OK marker
    at all), ``False`` if the candidate was cleared (OK marker present
    AND no negative markers).

    LCM Wave-4 Auditor #5 P0 (2026-01-12): the Wave-2 relaxation
    over-corrected — a response with BOTH "UNSUPPORTED: claim X" AND a
    stray "OK on the rest" matched the regex and CLEARED the
    hallucination flag. The verify pass is meant to FLAG hallucinations;
    this regression let hallucinated monthlies land in the cache as
    'ready'. Tighten: require absence of UNSUPPORTED / HALLUCINATION
    markers AS WELL AS presence of OK\\b. Either marker present → flagged.
    Original: ``lossless-claw/src/synthesis/dispatch.ts:351-354``.

    LCM Wave-1 Auditor #3 finding #4 (2025-11-08): anchored at
    start-of-string only meant any preamble ("Here is the fidelity
    report:\\n\\nOK: ...") false-positive flagged. Allow ``OK\\b`` at
    start-of-string OR start of any line.

    LCM Final.review.3 Loop 4 Bug 4.1 (2026-01-12): regex was
    ``^OK$`` (requires bare OK only), which rejected the seeded "OK:
    all N claims grounded" contract — every clean monthly synthesis
    was wrongly flagged as hallucinating. Relaxed to ``^OK\\b`` so "OK"
    on its own OR followed by a colon/whitespace passes.

    Args:
        output: The raw verify_fidelity LLM output.

    Returns:
        ``True`` if hallucinations were flagged (UNSUPPORTED/
        HALLUCINATION marker present OR no OK marker at all),
        ``False`` if the candidate was cleared.
    """

    has_negative = _VERIFY_NEGATIVE_RE.search(output) is not None
    has_ok = _VERIFY_OK_RE.search(output) is not None
    return has_negative or not has_ok


# ---------------------------------------------------------------------------
# Judge-output parsing
# ---------------------------------------------------------------------------


# Pre-compiled regex patterns for judge-output parsing.
_JUDGE_WINNER_RE = re.compile(r"(?:^|\b)Winner\s*[:\s]\s*(\d+)", re.IGNORECASE | re.MULTILINE)
_JUDGE_DIGITS_RE = re.compile(r"\d+")


def _parse_judge_output(output: str, n: int) -> int:
    """Parse the best_of_n_judge LLM output into a candidate index.

    Strategy (LCM Final.review.3 Loop 4 Bug 4.3, see Wave-N comment):

    1. Prefer the explicit ``Winner: N`` anchored form.
    2. Fall back to "scan backwards for last digit" — the model's final
       commitment is more robust than the first digit in its reasoning.
    3. Both bounded against ``n`` before accept.

    LCM Final.review.3 Loop 4 Bug 4.3 (2026-01-12): the previous regex
    ``/\\d+/`` matched the FIRST digit anywhere — including "0" from the
    prompt's "0-indexed" instruction echoed by the model, or a year
    "2026" appearing in the model's reasoning, or "12 monthlies" count.
    That made yearly synthesis silently wrong (wrong winner) or hard
    failure (out-of-range like 2026 or 12 vs N=3).
    Original: ``lossless-claw/src/synthesis/dispatch.ts:714-753``.

    Args:
        output: The raw judge LLM output.
        n: The number of candidates (range check upper bound).

    Returns:
        The candidate index in ``[0, n)``.

    Raises:
        SynthesisDispatchError: ``kind="judge_failure"`` if no parseable
        index found in range.
    """

    winner_match = _JUDGE_WINNER_RE.search(output)
    if winner_match is not None:
        idx = int(winner_match.group(1))
        if 0 <= idx < n:
            return idx
        # Winner: matched but out of range — fall through to last-digit fallback.

    all_digits = _JUDGE_DIGITS_RE.findall(output)
    if not all_digits:
        raise SynthesisDispatchError(
            "judge_failure",
            f"[synthesis.dispatch] judge output didn't contain a digit: {output[:200]}",
        )

    # Try last → second-to-last → ... → first, accept first one in range.
    for digit_str in reversed(all_digits):
        idx = int(digit_str)
        if 0 <= idx < n:
            return idx

    # No digit in output is in range — surface the FIRST out-of-range
    # one (matches old behavior for backward compat on error message).
    first_idx = int(all_digits[0])
    raise SynthesisDispatchError(
        "judge_failure",
        f"[synthesis.dispatch] judge picked out-of-range index {first_idx} "
        f"(N={n}); full output: {output[:200]}",
    )


# ---------------------------------------------------------------------------
# Audit row writes
# ---------------------------------------------------------------------------


def _insert_audit_row(
    db: sqlite3.Connection,
    *,
    audit_id: str,
    pass_session_id: str,
    target_summary_id: str | None,
    target_cache_id: str | None,
    prompt_id: str,
    pass_kind: PassKind,
    pass_input_truncated: str,
    status: Literal["started", "completed", "failed"],
    model_used: str,
) -> None:
    """Insert the ``'started'`` audit row.

    LCM Wave-9 (2026-03-08) audit insert BEFORE LLM call, UPDATE to
    ``completed`` / ``failed`` after. Forensic record survives crash
    between call and ack.
    Original: ``lossless-claw/src/synthesis/dispatch.ts:402``.
    """

    db.execute(
        "INSERT INTO lcm_synthesis_audit"
        " (audit_id, pass_session_id, target_summary_id, target_cache_id, prompt_id,"
        "  pass_kind, pass_input_truncated, status, model_used)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            audit_id,
            pass_session_id,
            target_summary_id,
            target_cache_id,
            prompt_id,
            pass_kind,
            pass_input_truncated,
            status,
            model_used,
        ),
    )


def _update_audit_row(
    db: sqlite3.Connection,
    audit_id: str,
    *,
    status: Literal["started", "completed", "failed"] | None = None,
    pass_output: str | None = None,
    model_used: str | None = None,
    latency_ms: int | None = None,
    cost_cents: int | None = None,
    last_error: str | None = None,
) -> None:
    """Update an audit row in place.

    Builds a dynamic ``SET`` clause from the non-``None`` kwargs. If no
    kwargs are set, the call is a no-op.

    Mirrors the TS ``updateAuditRow`` at ``dispatch.ts:491-536``.
    """

    sets: list[str] = []
    args: list[str | int] = []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if pass_output is not None:
        sets.append("pass_output = ?")
        args.append(pass_output)
    if model_used is not None:
        sets.append("model_used = ?")
        args.append(model_used)
    if latency_ms is not None:
        sets.append("latency_ms = ?")
        args.append(latency_ms)
    if cost_cents is not None:
        sets.append("cost_usd_cents = ?")
        args.append(cost_cents)
    if last_error is not None:
        sets.append("last_error = ?")
        args.append(last_error)
    if not sets:
        return
    args.append(audit_id)
    db.execute(
        f"UPDATE lcm_synthesis_audit SET {', '.join(sets)} WHERE audit_id = ?",
        args,
    )


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


#: Audit-row text-field hard cap (chars). Pass inputs + outputs longer
#: than this are truncated with a ``"…(truncated)"`` marker. Full inputs
#: / outputs are NOT retained on the audit row.
#:
#: TS source: ``dispatch.ts:809-810`` (``maxLen = 8000``).
_AUDIT_MAX_LEN: int = 8000

_AUDIT_TRUNCATED_MARKER: str = "…(truncated)"


def _truncate_for_audit(s: str, max_len: int = _AUDIT_MAX_LEN) -> str:
    """Truncate a string to ``max_len`` chars with ``"…(truncated)"`` marker.

    Used by both pass_input + pass_output recording. Full inputs / outputs
    are not retained.

    Mirrors the TS ``truncateForAudit`` at ``dispatch.ts:809-811``.

    Args:
        s: The string to truncate.
        max_len: Cap length. Default :data:`_AUDIT_MAX_LEN` (8000 chars).

    Returns:
        Either ``s`` unchanged (if shorter than ``max_len``) or
        ``s[:max_len] + "…(truncated)"``.
    """

    if len(s) > max_len:
        return s[:max_len] + _AUDIT_TRUNCATED_MARKER
    return s


def _random_suffix() -> str:
    """Return a 6-hex-char random suffix for audit-row IDs.

    ~24 bits of entropy from :func:`secrets.token_hex`. Used in
    ``audit_<pass_session_id>_<prompt_id[-6:]>_<suffix>`` IDs so
    concurrent dispatches within the same pass_session don't collide
    on PK.

    Mirrors the TS ``randomSuffix`` at ``dispatch.ts:813-817``. The TS
    version uses ``Math.random()``; we use :func:`secrets.token_hex` for
    cryptographic quality (no downside, no extra cost).
    """

    return secrets.token_hex(3)
