"""LCM summarizer prompt templates (issue 04-05).

This module is the byte-for-byte Python port of the three summarizer
prompt builders from
``lossless-claw/src/summarize.ts`` (commit ``1f07fbd``, branch ``pr-613``).

The prompt strings are **load-bearing operator tuning**: their exact
phrasing has been audited against summarizer output quality across the
LCM Wave-N audit series, and a "cleaner rewrite" will produce subtly
different summaries that may pass tests but degrade production quality.
Per ``docs/porting-guides/assembler-compaction.md`` §"Prompt templates",
the strings port verbatim — newlines, blank lines, bullet capitalization,
the literal ``"Files: none"`` and ``"Expand for details about:"`` end
markers, the ``(none)`` sentinel, and the XML wrapper tags
(``<previous_context>``, ``<conversation_segment>``,
``<conversation_to_condense>``) are all part of the contract.

SHA-256 snapshot tests under ``tests/test_summarize_prompts.py`` lock the
exact bytes so silent drift surfaces as a deliberate test-update, not a
quiet quality regression.

### Builders

* :data:`LCM_SUMMARIZER_SYSTEM_PROMPT` — the system-prompt constant the
  summarizer sends alongside every leaf/condensed builder output. TS
  source: ``summarize.ts:59-60``.
* :func:`build_leaf_prompt` — leaf-level (segment) summary prompt.
  Dispatches on ``mode`` (``"normal"`` vs ``"aggressive"``). TS source:
  ``summarize.ts:881-928`` (``buildLeafSummaryPrompt``).
* :func:`build_condensed_prompt` — condensed-tier prompt; dispatches on
  ``depth`` to one of three templates (D1 / D2 / D3+). TS source:
  ``summarize.ts:1052-1067`` (``buildCondensedSummaryPrompt``).
* :func:`build_deterministic_fallback` — deterministic non-LLM fallback
  used when the model is unavailable or returns empty output. Wave-4 P0
  invariant: ALWAYS includes the ``[LCM fallback summary — model
  unavailable; ...]`` marker prefix, even when the source fits within
  the char budget. Two distinct markers (Wave-9) let operators tell
  whether truncation also occurred. TS source: ``summarize.ts:1075-1102``
  (``buildDeterministicFallbackSummary``).

### Why "verbatim"

Per ADR-024 the literal string "OpenClaw" in the leaf prompt header
must survive the port — agents see "OpenClaw" in their compaction
summaries, and a port-time wording divergence to "Hermes" would shift
the agent's understanding of the context-compaction frame in ways the
operator tuning never accounted for. Other load-bearing details:

* Em dash (``—``) NOT hyphen (``-``) in the fallback marker.
* The literal ``"Files: none"`` instruction for the file-operations
  empty case.
* The ``"Expand for details about:"`` end marker.
* The ``<previous_context>``, ``<conversation_segment>``, and
  ``<conversation_to_condense>`` XML wrapper tags (parsed by downstream
  tooling).

See ``epics/04-compaction/04-05-summarize-prompt-templates.md`` for
the full acceptance criteria.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Literal, Mapping, Protocol

from lossless_hermes.estimate_tokens import estimate_tokens

__all__ = [
    "AUTH_ERROR_TEXT_PATTERN",
    "LCM_SUMMARIZER_SYSTEM_PROMPT",
    "LcmProviderAuthError",
    "LcmProviderResponseError",
    "LcmSummarizer",
    "LcmSummarizeOptions",
    "ProviderAuthFailure",
    "ProviderResponseFailure",
    "ResolvedSummaryCandidate",
    "SummarizerDeps",
    "SummarizerTimeoutError",
    "SummaryMode",
    "build_condensed_prompt",
    "build_deterministic_fallback",
    "build_leaf_prompt",
    "extract_provider_auth_failure",
    "extract_provider_response_failure",
    "normalize_completion_summary",
    "resolve_summary_candidates",
    "resolve_target_tokens",
]

logger = logging.getLogger("lossless_hermes.summarize")


# ---------------------------------------------------------------------------
# LcmProviderAuthError — auth-failure signal from the summarizer (issue 04-07)
# ---------------------------------------------------------------------------
#
# **Forward declaration for issue 04-06.** This issue (04-07 — circuit
# breaker integration) needs the error class to wire the catch around
# the engine's ``compact()`` entry point so consecutive auth failures
# can open the breaker. Issue 04-06 ports the full
# ``_summarize_with_escalation`` cascade that raises this error from the
# provider response path; 04-06 will extend (NOT replace) this class
# with the call-context attributes (provider, model, status_code, etc.)
# that the cascade carries.
#
# Maps to TS ``LcmProviderAuthError`` in ``lossless-claw/src/summarize.ts``
# (the class itself + the ``extractProviderAuthFailure`` helper at lines
# 525-560 that produces it). The TS shape: a tagged error with a
# ``kind: "provider_auth"`` discriminator and a ``status`` field
# (typically 401). The Python port subclasses :class:`RuntimeError` so
# callers can use plain ``try / except`` semantics without an
# error-discriminator dance — Python's class-based exception hierarchy
# already gives us the same dispatch.


@dataclass(frozen=True)
class ProviderAuthFailure:
    """Structured auth-failure signal extracted from a provider response or error.

    Maps to TS ``ProviderAuthFailure`` (``summarize.ts:84-88``). The
    ``missing_model_request_scope`` flag is load-bearing for
    OpenClaw runtime-managed auth providers (``model.request`` scope
    is the OAuth scope that the upstream runtime grants when the
    summarizer is talking to a Claude-hosted endpoint).
    """

    status_code: int | None = None
    message: str | None = None
    missing_model_request_scope: bool = False


@dataclass(frozen=True)
class ProviderResponseFailure:
    """Structured non-auth provider failure (4xx/5xx, finish=error, etc.).

    Maps to TS ``ProviderResponseFailure`` (``summarize.ts:90-95``).
    Distinct from :class:`ProviderAuthFailure` so the cascade can
    decide whether to retry on the same candidate (auth → try
    ``skip_model_auth`` path) versus advance to the next candidate
    (non-auth response error).
    """

    status_code: int | None = None
    finish_reason: str | None = None
    code: str | None = None
    message: str | None = None


class LcmProviderAuthError(RuntimeError):
    """Auth failure raised by the summarizer on a 401 / explicit provider-auth signal.

    Maps to TS ``LcmProviderAuthError`` in ``summarize.ts:101-117``.

    **History:** forward-declared in issue 04-07 (circuit-breaker
    integration) as a bare marker class so the engine's catch site at
    ``engine/compact.py`` could be wired before the full cascade landed.
    Issue 04-06 (this file) enriches it with the call-context attributes
    that the TS class carries: ``provider``, ``model``, and the
    structured :class:`ProviderAuthFailure`. The 04-07 catch sites that
    rely only on the class identity (``except LcmProviderAuthError``)
    continue to work unchanged — the new attributes are additive.

    Both forms of construction are supported:

    * ``LcmProviderAuthError(msg)`` — legacy "marker" form. Sets
      ``provider="(unknown)"``, ``model="(unknown)"``, and an empty
      :class:`ProviderAuthFailure`. Used by callers that don't have
      candidate context (e.g. early-construction tests).
    * ``LcmProviderAuthError(provider=..., model=..., failure=...)`` —
      full form. The cascade always uses this kwarg form so the
      auth-warning message rendered into ``__str__`` matches the TS
      ``buildProviderAuthWarning`` output verbatim.

    See:

    * ``epics/04-compaction/04-06-summarize-fallback-chain.md`` —
      defines this enriched error surface.
    * ``epics/04-compaction/04-07-circuit-breaker-integration.md`` —
      catches it to open the breaker.
    * TS source: ``lossless-claw/src/summarize.ts:101-117``
      (commit ``1f07fbd``).
    """

    provider: str
    model: str
    failure: ProviderAuthFailure

    def __init__(
        self,
        message: str | None = None,
        *,
        provider: str | None = None,
        model: str | None = None,
        failure: ProviderAuthFailure | None = None,
    ) -> None:
        # Resolve the call-context attributes. The marker form (no
        # kwargs) accepts a plain message string and synthesizes
        # placeholder attributes; the full form requires all three
        # kwargs and synthesizes the warning message from them.
        self.provider = provider or "(unknown)"
        self.model = model or "(unknown)"
        self.failure = failure or ProviderAuthFailure()
        if message is None:
            message = _build_provider_auth_warning(
                provider=self.provider, model=self.model, failure=self.failure
            )
        super().__init__(message)


class LcmProviderResponseError(RuntimeError):
    """Non-auth provider failure (4xx / 5xx / finish=error|failed|cancelled).

    Maps to TS ``LcmProviderResponseError`` (``summarize.ts:120-136``).
    Raised by the candidate-attempt path when the provider returned a
    response envelope carrying a structural error signal (e.g.
    ``finish_reason: "error"``, ``status >= 400``, or an ``error.kind``
    other than ``"provider_auth"``).

    The cascade catches this distinctly from :class:`LcmProviderAuthError`
    because:

    * Auth errors trigger the ``skip_model_auth`` retry path before
      advancing to the next candidate.
    * Non-auth response errors advance directly to the next candidate.
    * Both apply the same exponential backoff between candidates.
    """

    provider: str
    model: str
    failure: ProviderResponseFailure

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        failure: ProviderResponseFailure,
    ) -> None:
        self.provider = provider
        self.model = model
        self.failure = failure
        super().__init__(
            _build_provider_response_warning(provider=provider, model=model, failure=failure)
        )


class SummarizerTimeoutError(RuntimeError):
    """Per-call timeout for the summarizer LLM call (default 60s).

    Maps to TS ``SummarizerTimeoutError`` (``summarize.ts:146-151``).
    Distinct from generic :class:`TimeoutError` and from provider
    failures so the cascade can:

    * On the LAST candidate timeout → return a deterministic fallback
      (don't raise — keep compaction progress monotonic).
    * On NON-LAST candidate timeout → advance to the next candidate
      with exponential backoff.

    Per ADR-017, the Python port uses
    :class:`concurrent.futures.ThreadPoolExecutor` with
    ``Future.result(timeout=...)`` to implement the timeout — NOT
    ``asyncio.wait_for``. The LCM port keeps the summarizer sync to
    match Hermes's :func:`auxiliary_client.call_llm` contract.
    """

    def __init__(self, ms: int, label: str) -> None:
        super().__init__(f"[lcm] summarizer timeout after {ms}ms ({label})")
        self.timeout_ms = ms
        self.label = label


def _build_provider_auth_warning(*, provider: str, model: str, failure: ProviderAuthFailure) -> str:
    """Render the auth-failure warning text.

    Verbatim port of TS ``buildProviderAuthWarning``
    (``summarize.ts:562-583``). The exact phrasing is load-bearing for
    operator-facing log output — the message appears in ``/lcm health``
    output and operator runbooks. Tests pin the substrings.
    """
    detail_parts: list[str] = []
    if failure.status_code == 401:
        detail_parts.append("401")
    if failure.missing_model_request_scope:
        detail_parts.append("missing model.request scope")
    if detail_parts:
        detail = f"provider auth error ({' / '.join(detail_parts)})"
    else:
        detail = "provider auth error"
    if failure.message and not failure.missing_model_request_scope:
        message_suffix = f" Detail: {failure.message}"
    else:
        message_suffix = ""
    return (
        f"[lcm] compaction failed: {detail}. Check that the configured "
        f"summaryProvider has valid API credentials. "
        f"Current: {provider}/{model}{message_suffix}"
    )


def _build_provider_response_warning(
    *, provider: str, model: str, failure: ProviderResponseFailure
) -> str:
    """Render the non-auth response-failure warning text.

    Verbatim port of TS ``buildProviderResponseWarning``
    (``summarize.ts:654-672``).
    """
    detail_parts: list[str] = []
    if failure.status_code is not None:
        detail_parts.append(str(failure.status_code))
    if failure.finish_reason:
        detail_parts.append(f"finish={failure.finish_reason}")
    if failure.code:
        detail_parts.append(f"code={failure.code}")
    detail = f" ({' / '.join(detail_parts)})" if detail_parts else ""
    message_suffix = f" Detail: {failure.message}" if failure.message else ""
    return (
        f"[lcm] provider error response{detail}; provider={provider}; model={model}{message_suffix}"
    )


# ---------------------------------------------------------------------------
# LCM_SUMMARIZER_SYSTEM_PROMPT
# ---------------------------------------------------------------------------
#
# TS source: ``lossless-claw/src/summarize.ts:59-60`` (commit ``1f07fbd``).
#
# Sent as the ``system`` parameter on every summarizer LLM call. The leaf
# and condensed user-prompt builders carry the per-call detail; this
# constant carries the global "you are a summarization engine" framing.

LCM_SUMMARIZER_SYSTEM_PROMPT: Final[str] = (
    "You are a context-compaction summarization engine. "
    "Follow user instructions exactly and return plain text summary content only."
)


SummaryMode = Literal["normal", "aggressive"]
"""Leaf-prompt mode dispatch — see :func:`build_leaf_prompt`."""


def _normalize_previous_context(previous_summary: str | None) -> str:
    """Apply the TS ``previousSummary?.trim() || "(none)"`` coercion.

    JavaScript's truthy-coercion semantics mean both ``undefined`` and
    a whitespace-only string fall through to the ``"(none)"`` sentinel.
    Python's ``None`` and ``str.strip() == ""`` cases map onto the same
    behavior so the prompt body is byte-identical to the TS form.
    """
    if previous_summary is None:
        return "(none)"
    stripped = previous_summary.strip()
    return stripped if stripped else "(none)"


def _instruction_block(custom_instructions: str | None) -> str:
    """Render the operator-instructions block.

    TS source: ``customInstructions?.trim() ? ... : "Operator instructions: (none)"``
    (``summarize.ts:906-908``, ``937-939``, ``986-988``, ``1022-1024``).
    The non-empty branch interpolates the *trimmed* text, so trailing
    whitespace from the operator is dropped before it lands in the
    prompt.
    """
    if custom_instructions is None:
        return "Operator instructions: (none)"
    stripped = custom_instructions.strip()
    if not stripped:
        return "Operator instructions: (none)"
    return f"Operator instructions:\n{stripped}"


# ---------------------------------------------------------------------------
# build_leaf_prompt — leaf-level (segment) summary
# ---------------------------------------------------------------------------
#
# TS source: ``lossless-claw/src/summarize.ts:881-928``
# (``buildLeafSummaryPrompt``, commit ``1f07fbd``).
#
# Used for first-tier (leaf) compaction of a single conversation
# segment. Normal mode preserves detail for follow-up turns; aggressive
# mode keeps only durable facts + current task state.


_LEAF_NORMAL_POLICY: Final[str] = "\n".join([
    "Normal summary policy:",
    "- Preserve key decisions, rationale, constraints, and active tasks.",
    "- Keep essential technical details needed to continue work safely.",
    "- Remove obvious repetition and conversational filler.",
])

_LEAF_AGGRESSIVE_POLICY: Final[str] = "\n".join([
    "Aggressive summary policy:",
    "- Keep only durable facts and current task state.",
    "- Remove examples, repetition, and low-value narrative details.",
    "- Preserve explicit TODOs, blockers, decisions, and constraints.",
])


def build_leaf_prompt(
    *,
    text: str,
    mode: SummaryMode,
    target_tokens: int,
    previous_summary: str | None = None,
    custom_instructions: str | None = None,
) -> str:
    """Build the leaf (segment) summarization prompt.

    Verbatim port of
    ``lossless-claw/src/summarize.ts:buildLeafSummaryPrompt``
    (lines 881-928, commit ``1f07fbd``).

    Args:
        text: The raw conversation segment to summarize. Interpolated
            inside the ``<conversation_segment>`` XML wrapper.
        mode: ``"normal"`` preserves details for follow-up turns;
            ``"aggressive"`` keeps only durable facts. Selects the
            policy block.
        target_tokens: Soft target length for the model's output. The
            integer is interpolated into the ``Target length: about N
            tokens or less.`` line — the model uses it as a length cue.
        previous_summary: Prior summary to seed continuity. ``None``
            (or whitespace-only) renders as the literal ``(none)``.
        custom_instructions: Operator-supplied additional guidance.
            ``None`` (or whitespace-only) renders as
            ``Operator instructions: (none)``.

    Returns:
        The assembled prompt. Top-level sections are joined with
        ``"\\n\\n"`` (one blank line between sections); the body of each
        bullet block is ``"\\n"`` separated.

    The header phrase "You summarize a SEGMENT of an OpenClaw
    conversation..." preserves the TS string verbatim per ADR-024 —
    agents see "OpenClaw" in their compaction summaries, and a
    port-time rename would shift the agent's framing of the
    compaction input in untested ways.
    """
    previous_context = _normalize_previous_context(previous_summary)
    policy = _LEAF_AGGRESSIVE_POLICY if mode == "aggressive" else _LEAF_NORMAL_POLICY
    instruction_block = _instruction_block(custom_instructions)

    output_requirements = "\n".join([
        "Output requirements:",
        "- Plain text only.",
        "- No preamble, headings, or markdown formatting.",
        "- Keep it concise while preserving required details.",
        "- Track file operations (created, modified, deleted, renamed) with file paths and current status.",
        '- If no file operations appear, include exactly: "Files: none".',
        '- End with exactly: "Expand for details about: <comma-separated list of what was dropped or compressed>".',
        f"- Target length: about {target_tokens} tokens or less.",
    ])

    return "\n\n".join([
        "You summarize a SEGMENT of an OpenClaw conversation for future model turns.",
        "Treat this as incremental memory compaction input, not a full-conversation summary.",
        policy,
        instruction_block,
        output_requirements,
        f"<previous_context>\n{previous_context}\n</previous_context>",
        f"<conversation_segment>\n{text}\n</conversation_segment>",
    ])


# ---------------------------------------------------------------------------
# build_condensed_prompt — condensed-tier dispatch
# ---------------------------------------------------------------------------
#
# TS source: ``lossless-claw/src/summarize.ts:1052-1067``
# (``buildCondensedSummaryPrompt``, commit ``1f07fbd``).
#
# Dispatches to one of three depth-specific templates:
#   depth <= 1  -> D1 (leaf summaries into a condensed memory node;
#                  includes <previous_context> block)
#   depth == 2  -> D2 (session-level into a higher-level node; no
#                  previous_context; "dates and approximate time of day"
#                  timeline directive)
#   depth >= 3  -> D3+ (high-level memory node from phase-level
#                  summaries; "date ranges" timeline directive)


def _build_d1_prompt(
    *,
    text: str,
    target_tokens: int,
    previous_summary: str | None,
    custom_instructions: str | None,
) -> str:
    """Build the D1 condensed prompt (depth ≤ 1).

    TS source: ``lossless-claw/src/summarize.ts:930-978``
    (``buildD1Prompt``, commit ``1f07fbd``).

    The D1 template uniquely includes a ``<previous_context>`` block
    when ``previous_summary`` is non-empty; D2 and D3+ do not. The
    timeline directive at D1 is "hour or half-hour" granularity.
    """
    instruction_block = _instruction_block(custom_instructions)
    previous_context = previous_summary.strip() if previous_summary else ""
    if previous_context:
        previous_context_block = "\n".join([
            "It already has this preceding summary as context. Do not repeat information",
            "that appears there unchanged. Focus on what is new, changed, or resolved:",
            "",
            f"<previous_context>\n{previous_context}\n</previous_context>",
        ])
    else:
        previous_context_block = "Focus on what matters for continuation:"

    body = "\n".join([
        "Preserve:",
        "- Decisions made and their rationale when rationale matters going forward.",
        "- Earlier decisions that were superseded, and what replaced them.",
        "- Completed tasks/topics with outcomes.",
        "- In-progress items with current state and what remains.",
        "- Blockers, open questions, and unresolved tensions.",
        "- Specific references (names, paths, URLs, identifiers) needed for continuation.",
        "",
        "Drop low-value detail:",
        "- Context that has not changed from previous_context.",
        "- Intermediate dead ends where the conclusion is already known.",
        "- Transient states that are already resolved.",
        "- Tool-internal mechanics and process scaffolding.",
        "",
        "Use plain text. No mandatory structure.",
        "Include a timeline with timestamps (hour or half-hour) for significant events.",
        "Present information chronologically and mark superseded decisions.",
        'End with exactly: "Expand for details about: <comma-separated list of what was dropped or compressed>".',
        f"Target length: about {target_tokens} tokens.",
    ])

    return "\n\n".join([
        "You are compacting leaf-level conversation summaries into a single condensed memory node.",
        "You are preparing context for a fresh model instance that will continue this conversation.",
        instruction_block,
        previous_context_block,
        body,
        f"<conversation_to_condense>\n{text}\n</conversation_to_condense>",
    ])


def _build_d2_prompt(
    *,
    text: str,
    target_tokens: int,
    custom_instructions: str | None,
) -> str:
    """Build the D2 condensed prompt (depth == 2).

    TS source: ``lossless-claw/src/summarize.ts:980-1014``
    (``buildD2Prompt``, commit ``1f07fbd``).

    D2 does NOT include a ``<previous_context>`` block; the timeline
    directive is "dates and approximate time of day" granularity
    (coarser than D1's hour/half-hour).
    """
    instruction_block = _instruction_block(custom_instructions)

    body = "\n".join([
        "Preserve:",
        "- Decisions still in effect and their rationale.",
        "- Decisions that evolved: what changed and why.",
        "- Completed work with outcomes.",
        "- Active constraints, limitations, and known issues.",
        "- Current state of in-progress work.",
        "",
        "Drop:",
        "- Session-local operational detail and process mechanics.",
        "- Identifiers that are no longer relevant.",
        "- Intermediate states superseded by later outcomes.",
        "",
        "Use plain text. Brief headers are fine if useful.",
        "Include a timeline with dates and approximate time of day for key milestones.",
        'End with exactly: "Expand for details about: <comma-separated list of what was dropped or compressed>".',
        f"Target length: about {target_tokens} tokens.",
    ])

    return "\n\n".join([
        "You are condensing multiple session-level summaries into a higher-level memory node.",
        "A future model should understand trajectory, not per-session minutiae.",
        instruction_block,
        body,
        f"<conversation_to_condense>\n{text}\n</conversation_to_condense>",
    ])


def _build_d3_plus_prompt(
    *,
    text: str,
    target_tokens: int,
    custom_instructions: str | None,
) -> str:
    """Build the D3+ condensed prompt (depth ≥ 3).

    TS source: ``lossless-claw/src/summarize.ts:1016-1050``
    (``buildD3PlusPrompt``, commit ``1f07fbd``).

    D3+ does NOT include a ``<previous_context>`` block; the timeline
    directive is "date ranges" granularity (coarsest). The depth is
    NOT interpolated — depth 3, 4, 5+ all share the same template.
    """
    instruction_block = _instruction_block(custom_instructions)

    body = "\n".join([
        "Preserve:",
        "- Key decisions and rationale.",
        "- What was accomplished and current state.",
        "- Active constraints and hard limitations.",
        "- Important relationships between people, systems, or concepts.",
        "- Durable lessons learned.",
        "",
        "Drop:",
        "- Operational and process detail.",
        "- Method details unless the method itself was the decision.",
        "- Specific references unless essential for continuation.",
        "",
        "Use plain text. Be concise.",
        "Include a brief timeline with dates (or date ranges) for major milestones.",
        'End with exactly: "Expand for details about: <comma-separated list of what was dropped or compressed>".',
        f"Target length: about {target_tokens} tokens.",
    ])

    return "\n\n".join([
        "You are creating a high-level memory node from multiple phase-level summaries.",
        "This may persist for the rest of the conversation. Keep only durable context.",
        instruction_block,
        body,
        f"<conversation_to_condense>\n{text}\n</conversation_to_condense>",
    ])


def build_condensed_prompt(
    *,
    text: str,
    target_tokens: int,
    depth: int,
    previous_summary: str | None = None,
    custom_instructions: str | None = None,
) -> str:
    """Build a condensed-tier summarization prompt.

    Verbatim port of
    ``lossless-claw/src/summarize.ts:buildCondensedSummaryPrompt``
    (lines 1052-1067, commit ``1f07fbd``).

    Dispatches to one of three depth-specific templates:

    * ``depth <= 1`` → :func:`_build_d1_prompt` (leaf-level → condensed
      memory node; includes ``<previous_context>``; hour/half-hour
      timeline).
    * ``depth == 2`` → :func:`_build_d2_prompt` (session-level →
      higher-level memory node; no ``previous_context``; dates +
      approximate time-of-day timeline).
    * ``depth >= 3`` → :func:`_build_d3_plus_prompt` (phase-level →
      high-level memory node; no ``previous_context``; date-range
      timeline).

    Note that ``previous_summary`` is forwarded only on the D1 path —
    the TS implementation drops it at D2 and D3+, on the rationale that
    higher-tier compaction is summarizing already-condensed material
    rather than continuing a fresh stream.

    Args:
        text: Source content to condense (will be wrapped in
            ``<conversation_to_condense>``).
        target_tokens: Soft target output length, interpolated into the
            ``Target length: about N tokens.`` line.
        depth: Output-node depth (drives the template selection).
        previous_summary: Only consumed at D1; ignored at D2 / D3+.
        custom_instructions: Operator guidance. ``None`` /
            whitespace-only renders as ``Operator instructions: (none)``.

    Returns:
        The assembled prompt.
    """
    if depth <= 1:
        return _build_d1_prompt(
            text=text,
            target_tokens=target_tokens,
            previous_summary=previous_summary,
            custom_instructions=custom_instructions,
        )
    if depth == 2:
        return _build_d2_prompt(
            text=text,
            target_tokens=target_tokens,
            custom_instructions=custom_instructions,
        )
    return _build_d3_plus_prompt(
        text=text,
        target_tokens=target_tokens,
        custom_instructions=custom_instructions,
    )


# ---------------------------------------------------------------------------
# build_deterministic_fallback — non-LLM fallback
# ---------------------------------------------------------------------------
#
# TS source: ``lossless-claw/src/summarize.ts:1075-1102``
# (``buildDeterministicFallbackSummary``, commit ``1f07fbd``).

# LCM Wave-9 (2026-03-08): two distinct markers (preserved-verbatim
# vs truncated) so operators reading /lcm health, doctor scans, or
# eval reports can tell whether truncation also occurred during the
# fallback path. Previously a single marker hid the truncation case.
# Original: lossless-claw/src/summarize.ts:1091-1094.
_FALLBACK_MARKER: Final[str] = (
    "[LCM fallback summary — model unavailable; raw source preserved verbatim below]"
)
_FALLBACK_MARKER_TRUNC: Final[str] = (
    "[LCM fallback summary — model unavailable; raw source truncated for context management]"
)


def build_deterministic_fallback(text: str, target_tokens: int) -> str:
    """Return a deterministic non-LLM fallback summary.

    Verbatim port of
    ``lossless-claw/src/summarize.ts:buildDeterministicFallbackSummary``
    (lines 1075-1102, commit ``1f07fbd``).

    Used when the model is unavailable, the circuit breaker is open, or
    the LLM returned empty output. Keeps compaction progress monotonic
    instead of throwing and aborting the whole compaction pass.

    Empty / whitespace-only input returns the empty string (matching
    the TS ``if (!trimmed) return ""`` short-circuit at line 1078).

    Args:
        text: The raw source content. Stripped before length checks.
        target_tokens: Soft target. Drives ``max_chars = max(256,
            target_tokens * 4)`` — the 256 floor is load-bearing for
            very small targets where ``target_tokens * 4`` would
            otherwise truncate too aggressively.

    Returns:
        ``""`` if ``text`` is None / whitespace-only.

        Otherwise a string of the form ``"<marker>\\n<source>"`` where
        ``<marker>`` is one of two Wave-9 variants and ``<source>`` is
        either the stripped text verbatim (if it fits) or the stripped
        text truncated to ``max_chars``.

    The marker is ALWAYS present, even for short text where
    ``len(text) <= max_chars`` — this is the Wave-4 P0 invariant.
    """
    # The TS implementation guards on `typeof text !== "string"`. In
    # Python the type system prevents the non-string case; the empty /
    # whitespace-only short-circuit below is the load-bearing branch.
    if not text:
        return ""
    trimmed = text.strip()
    if not trimmed:
        return ""

    # LCM Wave-4 (2026-01-18): ALWAYS tag fallback output with a marker,
    # even when the source text is short enough to fit within
    # target_tokens. Without this, the under-cap branch returned the raw
    # source verbatim with no marker — downstream tiers in the pyramid
    # would treat raw user/tool content as a compacted summary, hiding
    # the fact that the LLM summarizer was unavailable. Operators
    # reading /lcm health, doctor scans, or eval reports could not
    # distinguish "LLM down, fallback shipped raw content" from "LLM
    # ran cleanly, summary is the source". Now both branches carry an
    # explicit marker.
    # Original: lossless-claw/src/summarize.ts:1082-1094.
    max_chars = max(256, target_tokens * 4)
    if len(trimmed) <= max_chars:
        return f"{_FALLBACK_MARKER}\n{trimmed}"
    return f"{_FALLBACK_MARKER_TRUNC}\n{trimmed[:max_chars]}"


# =============================================================================
# Issue 04-06 — fallback chain
# =============================================================================
#
# Below is the body that ports the LCM ``createLcmSummarizeFromLegacyParams``
# cascade (``summarize.ts:1258-1696``). Builds the 5-layer provider/model
# candidate resolution, the per-candidate retry loop, the auth/timeout
# distinguishing paths, and the deterministic fallback.
#
# TS source pin: ``lossless-claw/src/summarize.ts`` (commit ``1f07fbd``).


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SUMMARIZER_TIMEOUT_MS: Final[int] = 60_000
"""Default per-call timeout (matches TS ``DEFAULT_SUMMARIZER_TIMEOUT_MS``)."""

DEFAULT_LEAF_TARGET_TOKENS: Final[int] = 4000
DEFAULT_CONDENSED_TARGET_TOKENS: Final[int] = 2000

# Diagnostic-output limits — match TS constants so log output is parity.
_DIAGNOSTIC_MAX_DEPTH: Final[int] = 4
_DIAGNOSTIC_MAX_ARRAY_ITEMS: Final[int] = 8
_DIAGNOSTIC_MAX_OBJECT_KEYS: Final[int] = 16
_DIAGNOSTIC_MAX_CHARS: Final[int] = 1200

# Verbatim port of TS ``AUTH_ERROR_TEXT_PATTERN`` (``summarize.ts:67``).
# Used in the non-structural detection path (``require_structural_signal=False``)
# to catch caught errors that carry plain-text auth markers.
AUTH_ERROR_TEXT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b401\b"
    r"|unauthorized"
    r"|unauthorised"
    r"|invalid[_ -]?token"
    r"|invalid[_ -]?api[_ -]?key"
    r"|authentication failed"
    r"|authorization failed"
    r"|missing scope"
    r"|insufficient scope"
    r"|model\.request\b",
    re.IGNORECASE,
)

_AUTH_ERROR_STATUS_KEYS: Final[tuple[str, ...]] = ("status", "statusCode", "status_code")
_AUTH_ERROR_NESTED_KEYS: Final[tuple[str, ...]] = (
    "error",
    "response",
    "cause",
    "details",
    "data",
    "body",
)
_AUTH_ERROR_TOP_LEVEL_KEYS: Final[tuple[str, ...]] = (
    "error",
    "errorMessage",
    "status",
    "statusCode",
    "status_code",
    "code",
    "details",
    "cause",
    "data",
    "body",
)


SummaryDepth = int
"""Output-node depth for condensed prompts (1 → D1, 2 → D2, ≥3 → D3+)."""


@dataclass(frozen=True)
class LcmSummarizeOptions:
    """Per-call options for :meth:`LcmSummarizer.summarize`.

    Maps to TS ``LcmSummarizeOptions`` (``summarize.ts:5-9``).
    """

    previous_summary: str | None = None
    is_condensed: bool = False
    depth: SummaryDepth | None = None


@dataclass(frozen=True)
class ResolvedSummaryCandidate:
    """One ``(provider, model)`` candidate in the 5-layer resolution chain.

    Maps to TS ``ResolvedSummaryCandidate`` (``summarize.ts:33-36``).
    The ``level_name`` is operator-facing (rendered in log lines on
    "PROVIDER FALLBACK" transitions). ``use_legacy_auth_profile``
    distinguishes the legacy-runtime layer from the four explicit
    config-driven layers.
    """

    level_name: str
    model: str
    provider: str
    has_explicit_provider: bool = False
    use_legacy_auth_profile: bool = False


class SummarizerDeps(Protocol):
    """Dependency surface for :class:`LcmSummarizer`.

    Maps to the subset of TS ``LcmDependencies`` (``types.ts:115``) that
    the summarizer cascade actually touches. Defined as a
    :class:`typing.Protocol` so callers can supply a fake in tests
    without subclassing.

    Required methods:

    * :meth:`complete` — call the underlying LLM. Returns a result dict
      shaped like the TS provider envelope (``content``, optional
      ``error``, ``finish_reason``, etc.).
    * :meth:`get_api_key` — fetch the credential for ``(provider,
      model)``. ``skip_model_auth=True`` bypasses runtime.modelAuth and
      uses direct credentials (the auth-retry path).
    * :meth:`is_runtime_managed_auth_provider` — OPTIONAL guard. When
      True, the ``skip_model_auth`` retry is suppressed (OAuth-managed
      providers cannot use the bypass).
    """

    def complete(
        self,
        *,
        provider: str,
        model: str,
        api_key: str | None,
        system: str,
        user_prompt: str,
        max_tokens: int,
        reasoning: str | None = None,
        skip_model_auth: bool = False,
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        """Invoke the underlying LLM and return the provider envelope."""
        ...

    def get_api_key(
        self,
        provider: str,
        model: str,
        *,
        skip_model_auth: bool = False,
    ) -> str | None:
        """Resolve the API key for ``(provider, model)`` or return ``None``."""
        ...

    def is_runtime_managed_auth_provider(self, provider: str) -> bool:
        """Whether ``provider`` uses runtime-managed OAuth (e.g. claude-runtime).

        When True the cascade SKIPS the ``skip_model_auth`` retry path —
        runtime-managed providers cannot use direct credentials, and
        attempting to bypass would surface a misleading "no credentials
        found" error to the operator. Default callers can leave this
        method out (the cascade treats absence as ``return False``).
        """
        ...


# ---------------------------------------------------------------------------
# resolve_target_tokens — TS ``resolveTargetTokens`` (lines 855-873)
# ---------------------------------------------------------------------------


def resolve_target_tokens(
    *,
    input_tokens: int,
    mode: SummaryMode,
    is_condensed: bool,
    leaf_target_tokens: int = DEFAULT_LEAF_TARGET_TOKENS,
    condensed_target_tokens: int = DEFAULT_CONDENSED_TARGET_TOKENS,
) -> int:
    """Resolve the target token count for a leaf or condensed summary call.

    Verbatim port of TS ``resolveTargetTokens`` (``summarize.ts:855-873``).

    Behavior:

    * Condensed → ``max(512, condensed_target_tokens)``.
    * Leaf normal → ``max(192, min(leaf_target_tokens, floor(input_tokens
      * 0.35)))``.
    * Leaf aggressive → ``max(96, min(aggressive_cap, floor(input_tokens
      * 0.20)))`` where ``aggressive_cap = max(96, min(leaf_target_tokens,
      floor(leaf_target_tokens * 0.55)))``.

    The 192/96/512 floors are load-bearing for very small inputs — a
    naive ``min(leaf_target_tokens, input_tokens * 0.35)`` would cap a
    1-token input's target at 0 tokens. Floors guarantee the model has
    enough budget to emit the required ``Files: none`` and ``Expand for
    details about: ...`` end-markers regardless of input size.
    """
    if is_condensed:
        return max(512, condensed_target_tokens)

    leaf_floor = max(192, leaf_target_tokens)
    if mode == "aggressive":
        aggressive_cap = max(96, min(leaf_floor, math.floor(leaf_floor * 0.55)))
        return max(96, min(aggressive_cap, math.floor(input_tokens * 0.20)))
    return max(192, min(leaf_floor, math.floor(input_tokens * 0.35)))


# ---------------------------------------------------------------------------
# _with_timeout — ADR-017 sync wrapper
# ---------------------------------------------------------------------------
#
# Per ADR-017, the Python port uses ``ThreadPoolExecutor`` +
# ``Future.result(timeout=...)`` instead of ``asyncio.wait_for``. LCM's
# summarize is sync (matches Hermes's ``auxiliary_client.call_llm``
# contract) and ``asyncio.wait_for`` would force the entire call chain
# async. The thread hop is cheap (microseconds compared to multi-second
# LLM calls) and keeps the function body sync end-to-end.


def _with_timeout(
    callable_: Callable[[], Any],
    *,
    timeout_ms: int,
    label: str,
) -> Any:
    """Run ``callable_`` with a wall-clock timeout, raising on overrun.

    Implements the sync equivalent of TS ``withTimeout``
    (``summarize.ts:153-161``) per ADR-017 §Consequences:
    ``ThreadPoolExecutor`` + ``Future.result(timeout=...)``, NOT
    ``asyncio.wait_for``.

    The pattern uses a single-worker executor that is closed cleanly
    on success / timeout. On timeout, the underlying call MAY continue
    running in its thread — the LLM client is responsible for honoring
    cancellation if needed (typically via the HTTP client's own timeout
    knob, which should match ``timeout_ms``).
    """
    timeout_s = timeout_ms / 1000.0
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(callable_)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError as exc:
            raise SummarizerTimeoutError(timeout_ms, label) from exc


# ---------------------------------------------------------------------------
# Normalization helpers — TS ``normalizeCompletionSummary`` + friends
# ---------------------------------------------------------------------------


def _is_reasoning_like_type(value: Any) -> bool:
    """Return True if a content block's ``type`` is reasoning/thinking.

    Maps to TS ``isReasoningLikeType`` (``summarize.ts:260-266``).
    Reasoning and thinking blocks are diagnostic — the cascade
    deliberately drops them from the summary text to avoid leaking
    chain-of-thought into the persisted output.
    """
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return "reasoning" in normalized or "thinking" in normalized


def _append_text_value(value: Any, out: list[str]) -> None:
    """Append raw textual values + nested ``value`` / ``text`` wrappers.

    Maps to TS ``appendTextValue`` (``summarize.ts:295-316``).
    """
    if isinstance(value, str):
        out.append(value)
        return
    if isinstance(value, list):
        for entry in value:
            _append_text_value(entry, out)
        return
    if not isinstance(value, dict):
        return
    inner_value = value.get("value")
    if isinstance(inner_value, str):
        out.append(inner_value)
    inner_text = value.get("text")
    if isinstance(inner_text, str):
        out.append(inner_text)


def _collect_text_like_fields(value: Any, out: list[str]) -> None:
    """Walk ``value`` collecting text payloads from common provider shapes.

    Maps to TS ``collectTextLikeFields`` (``summarize.ts:269-292``).
    Skips reasoning/thinking blocks by their ``type`` discriminator.
    """
    if isinstance(value, list):
        for entry in value:
            _collect_text_like_fields(entry, out)
        return
    if not isinstance(value, dict):
        return

    if _is_reasoning_like_type(value.get("type")):
        return

    for key in ("text", "output_text"):
        if key in value:
            _append_text_value(value[key], out)
    for key in ("content", "summary", "output", "message", "response"):
        if key in value:
            _collect_text_like_fields(value[key], out)


def _collect_block_types(value: Any, out: set[str]) -> None:
    """Collect block ``type`` labels for diagnostic logging.

    Maps to TS ``collectBlockTypes`` (``summarize.ts:240-257``).
    """
    if isinstance(value, list):
        for entry in value:
            _collect_block_types(entry, out)
        return
    if not isinstance(value, dict):
        return
    type_value = value.get("type")
    if isinstance(type_value, str) and type_value.strip():
        out.add(type_value.strip())
    for nested in value.values():
        _collect_block_types(nested, out)


def _normalize_text_fragments(chunks: list[str]) -> str:
    """Deduplicate exact fragments preserving first-seen order, join with newlines.

    Maps to TS ``normalizeTextFragments`` (``summarize.ts:224-237``).
    First-seen wins so providers that mirror output (e.g. ``content``
    and ``output_text`` both carry the same text) don't double the
    summary length.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        trimmed = chunk.strip()
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        normalized.append(trimmed)
    return "\n".join(normalized).strip()


def normalize_completion_summary(content: Any) -> tuple[str, list[str]]:
    """Normalize a provider completion-block payload into a text summary.

    Maps to TS ``normalizeCompletionSummary`` (``summarize.ts:319-331``).

    Returns ``(summary, sorted_block_types)``. The ``content`` argument
    is either a list of provider content blocks (most common) or a full
    response envelope (for envelope-aware extraction — see the cascade's
    fallback path).

    Reasoning/thinking blocks are dropped via :func:`_is_reasoning_like_type`.
    Exact duplicate fragments are deduplicated first-seen-wins via
    :func:`_normalize_text_fragments`.
    """
    chunks: list[str] = []
    block_type_set: set[str] = set()

    _collect_text_like_fields(content, chunks)
    _collect_block_types(content, block_type_set)

    block_types = sorted(block_type_set)
    return _normalize_text_fragments(chunks), block_types


# ---------------------------------------------------------------------------
# Auth detection helpers — TS ``extractProviderAuthFailure`` + friends
# ---------------------------------------------------------------------------


def _truncate_diagnostic_text(value: str, max_chars: int = _DIAGNOSTIC_MAX_CHARS) -> str:
    """Truncate long diagnostic text values to keep logs bounded.

    Maps to TS ``truncateDiagnosticText`` (``summarize.ts:342-347``).
    """
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}...[truncated:{len(value) - max_chars} chars]"


def _collect_auth_failure_text(value: Any, out: list[str], depth: int = 0) -> None:
    """Walk ``value`` collecting textual fields for auth-pattern matching.

    Maps to TS ``collectAuthFailureText`` (``summarize.ts:413-437``).
    Depth-limited to avoid pathological recursion on circular envelopes.
    """
    if depth >= 4:
        return
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            out.append(trimmed)
        return
    if isinstance(value, list):
        for entry in value[:_DIAGNOSTIC_MAX_ARRAY_ITEMS]:
            _collect_auth_failure_text(entry, out, depth + 1)
        return
    if not isinstance(value, dict):
        return
    for entry in list(value.values())[:_DIAGNOSTIC_MAX_OBJECT_KEYS]:
        _collect_auth_failure_text(entry, out, depth + 1)


def _extract_auth_failure_status_code(value: Any, depth: int = 0) -> int | None:
    """Walk ``value`` looking for a structural HTTP-status-code field.

    Maps to TS ``extractAuthFailureStatusCode`` (``summarize.ts:439-466``).
    Checks the well-known keys (``status``, ``statusCode``, ``status_code``)
    first, then recurses into nested envelopes (``error``, ``response``,
    ``cause``, etc.).
    """
    if depth >= 4 or not isinstance(value, dict):
        return None

    for key in _AUTH_ERROR_STATUS_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, bool):
            # Boolean is a subclass of int in Python; skip.
            continue
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, float) and math.isfinite(candidate):
            return math.trunc(candidate)
        if isinstance(candidate, str):
            try:
                return int(candidate, 10)
            except (TypeError, ValueError):
                continue

    for key in _AUTH_ERROR_NESTED_KEYS:
        nested = value.get(key)
        status_code = _extract_auth_failure_status_code(nested, depth + 1)
        if status_code is not None:
            return status_code

    return None


def _has_top_level_auth_inspection_keys(value: Mapping[str, Any]) -> bool:
    """Return True if any ``_AUTH_ERROR_TOP_LEVEL_KEYS`` is present.

    Maps to TS ``hasTopLevelAuthInspectionKeys`` (``summarize.ts:468-470``).
    """
    return any(key in value for key in _AUTH_ERROR_TOP_LEVEL_KEYS)


def _looks_like_thrown_error(value: Mapping[str, Any]) -> bool:
    """Heuristic — does this dict look like a thrown error envelope?

    Maps to TS ``looksLikeThrownError`` (``summarize.ts:472-481``). Used
    to gate the ``message`` field's inclusion in auth inspection — a
    successful summary response also carries a ``message`` field, so we
    only inspect ``message`` when the envelope is otherwise error-shaped.
    """
    name = value.get("name")
    if isinstance(name, str) and re.search(r"\berror\b", name, re.IGNORECASE):
        return True
    if "stack" in value:
        return True
    if isinstance(value.get("message"), str):
        if "content" not in value and "response" not in value and "output" not in value:
            return True
    return False


def _pick_auth_inspection_value(value: Any) -> Any:
    """Project the dict down to fields relevant for auth-failure inspection.

    Maps to TS ``pickAuthInspectionValue`` (``summarize.ts:483-522``).
    For non-dict input returns ``value`` directly. For dict input
    extracts the auth-relevant top-level keys + (conditionally)
    ``message`` and ``response`` to avoid false-positive matches on
    legitimate summary content.
    """
    if not isinstance(value, dict):
        return value

    error_field = value.get("error")
    if isinstance(error_field, dict) and error_field.get("kind") == "provider_auth":
        return error_field

    subset: dict[str, Any] = {}
    has_top_level_auth_keys = _has_top_level_auth_inspection_keys(value)
    error_like = isinstance(value, Exception) or _looks_like_thrown_error(value)

    for key in _AUTH_ERROR_TOP_LEVEL_KEYS:
        if key in value:
            subset[key] = value[key]

    if (has_top_level_auth_keys or error_like) and "message" in value:
        subset["message"] = value["message"]

    if "response" in value:
        response = value["response"]
        if (
            has_top_level_auth_keys
            or (isinstance(response, dict) and _has_top_level_auth_inspection_keys(response))
            or (isinstance(response, dict) and _looks_like_thrown_error(response))
        ):
            subset["response"] = response

    return subset if subset else {}


def _coerce_to_dict(value: Any) -> Mapping[str, Any] | None:
    """Coerce ``value`` to a ``Mapping[str, Any]`` view for inspection.

    For plain dicts → return as-is. For :class:`Exception` instances →
    return a synthesized view exposing ``message``, ``name``, and any
    attribute attached by the LLM client (e.g. ``status``,
    ``response``). For everything else → return ``None``.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, Exception):
        view: dict[str, Any] = {
            "name": type(value).__name__,
            "message": str(value),
        }
        # Pull well-known attrs from common HTTP-error shapes (httpx,
        # requests, anthropic, openai). Each library exposes a slightly
        # different attr name; map them all into the canonical keys.
        for attr in (
            "status",
            "status_code",
            "statusCode",
            "response",
            "cause",
            "details",
            "data",
            "body",
            "code",
            "error",
            "errorMessage",
            "stack",
        ):
            if hasattr(value, attr):
                attr_value = getattr(value, attr)
                # Skip method handles and class attributes.
                if callable(attr_value):
                    continue
                view[attr] = attr_value
        return view
    return None


def extract_provider_auth_failure(
    value: Any,
    *,
    require_structural_signal: bool = False,
) -> ProviderAuthFailure | None:
    """Detect a provider-auth failure in a response envelope or thrown error.

    Verbatim port of TS ``extractProviderAuthFailure``
    (``summarize.ts:525-560``).

    Behavior:

    * ``require_structural_signal=True`` (the success-path call) →
      ONLY returns a failure on HTTP 401 OR explicit ``error.kind ==
      "provider_auth"``. Plain text matches are NOT sufficient — the
      LLM summary may legitimately discuss auth errors.
    * ``require_structural_signal=False`` (the caught-error path) →
      Also matches scope signals (``model.request``, ``missing scope``,
      ``insufficient scope``) and the general
      :data:`AUTH_ERROR_TEXT_PATTERN`.
    """
    inspect_value = _pick_auth_inspection_value(_coerce_to_dict(value) or value)
    if inspect_value is None:
        return None
    status_code = _extract_auth_failure_status_code(inspect_value)
    text_parts: list[str] = []
    _collect_auth_failure_text(inspect_value, text_parts)
    normalized_message = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
    missing_model_request_scope = bool(
        re.search(r"\bmodel\.request\b", normalized_message, re.IGNORECASE)
    )
    has_scope_signal = missing_model_request_scope or bool(
        re.search(
            r"\b(missing|insufficient)\s+scope\b",
            normalized_message,
            re.IGNORECASE,
        )
    )

    coerced = _coerce_to_dict(value)
    has_explicit_error_kind = bool(
        isinstance(coerced, Mapping)
        and isinstance(coerced.get("error"), dict)
        and coerced["error"].get("kind") == "provider_auth"
    )

    if require_structural_signal:
        if status_code != 401 and not has_explicit_error_kind:
            return None
    elif (
        status_code != 401
        and not has_scope_signal
        and not AUTH_ERROR_TEXT_PATTERN.search(normalized_message)
    ):
        return None

    message: str | None
    if normalized_message:
        message = _truncate_diagnostic_text(normalized_message, 240)
    else:
        message = None
    return ProviderAuthFailure(
        status_code=status_code,
        message=message,
        missing_model_request_scope=missing_model_request_scope,
    )


def _get_provider_response_finish_reason(value: Mapping[str, Any]) -> str | None:
    """Extract the finish reason from a response envelope.

    Maps to TS ``getProviderResponseFinishReason`` (``summarize.ts:585-593``).
    """
    for key in ("finish_reason", "stopReason", "stop_reason", "status"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _get_provider_response_error_code(value: Mapping[str, Any]) -> str | None:
    """Extract a provider error-code string from a response envelope.

    Maps to TS ``getProviderResponseErrorCode`` (``summarize.ts:595-603``).
    """
    code = value.get("code")
    if isinstance(code, str) and code.strip():
        return code.strip()
    error = value.get("error")
    if isinstance(error, dict):
        inner_code = error.get("code")
        if isinstance(inner_code, str) and inner_code.strip():
            return inner_code.strip()
    return None


def _get_provider_response_error_message(value: Mapping[str, Any]) -> str | None:
    """Extract a provider error-message string from a response envelope.

    Maps to TS ``getProviderResponseErrorMessage`` (``summarize.ts:605-619``).
    """
    text_parts: list[str] = []
    for key in ("errorMessage", "message"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            text_parts.append(candidate.strip())
    error = value.get("error")
    if isinstance(error, dict):
        _collect_auth_failure_text(error, text_parts)
    if not text_parts:
        return None
    return _truncate_diagnostic_text(re.sub(r"\s+", " ", " ".join(text_parts)).strip(), 240)


def extract_provider_response_failure(value: Any) -> ProviderResponseFailure | None:
    """Detect a non-auth response failure in a provider envelope.

    Verbatim port of TS ``extractProviderResponseFailure``
    (``summarize.ts:621-652``).

    Triggers on:

    * ``finish_reason`` in ``{"error", "failed", "cancelled"}``.
    * HTTP status code ≥ 400.
    * Nested ``error.kind`` that is NOT ``"provider_auth"`` (the
      provider-auth case is handled by :func:`extract_provider_auth_failure`).

    Plain ``errorMessage`` strings on otherwise-successful envelopes do
    NOT trigger — the conservative retry path handles overloaded-empty-
    response cases separately.
    """
    if not isinstance(value, Mapping):
        return None

    status_code = _extract_auth_failure_status_code(value)
    finish_reason = _get_provider_response_finish_reason(value)
    normalized_finish = finish_reason.lower() if finish_reason else None
    nested_error = value.get("error")
    nested_error_kind: str | None = None
    if isinstance(nested_error, dict):
        kind = nested_error.get("kind")
        if isinstance(kind, str):
            nested_error_kind = kind

    has_explicit_error_signal = (
        normalized_finish in ("error", "failed", "cancelled")
        or (status_code is not None and status_code >= 400)
        or (nested_error_kind is not None and nested_error_kind != "provider_auth")
    )
    if not has_explicit_error_signal:
        return None

    return ProviderResponseFailure(
        status_code=status_code,
        finish_reason=finish_reason,
        code=_get_provider_response_error_code(value),
        message=_get_provider_response_error_message(value),
    )


# ---------------------------------------------------------------------------
# Incomplete-response signals — TS lines 808-849
# ---------------------------------------------------------------------------


def _collect_incomplete_response_signals(
    value: Any, out: set[str], label: str = "response", depth: int = 0
) -> None:
    """Walk ``value`` collecting incomplete-response markers.

    Maps to TS ``collectIncompleteResponseSignals``
    (``summarize.ts:808-842``).
    """
    if depth >= _DIAGNOSTIC_MAX_DEPTH:
        return
    if isinstance(value, list):
        for index, entry in enumerate(value[:_DIAGNOSTIC_MAX_ARRAY_ITEMS]):
            _collect_incomplete_response_signals(entry, out, f"{label}[{index}]", depth + 1)
        return
    if not isinstance(value, dict):
        return

    status = value.get("status")
    if isinstance(status, str) and status.strip().lower() == "incomplete":
        out.add(f"{label}.status=incomplete")
    incomplete_details = value.get("incomplete_details")
    if isinstance(incomplete_details, dict):
        reason = incomplete_details.get("reason")
        if isinstance(reason, str) and reason.strip():
            out.add(f"{label}.reason={reason.strip()}")

    for key in ("content", "output", "message", "response", "items"):
        if key in value:
            _collect_incomplete_response_signals(value[key], out, f"{label}.{key}", depth + 1)


def extract_incomplete_response_signals(value: Any) -> list[str]:
    """Return sorted list of incomplete-response signals.

    Maps to TS ``extractIncompleteResponseSignals``
    (``summarize.ts:845-849``). Used by the cascade to trigger a
    conservative ``reasoning="low"`` retry when the initial pass
    returned non-empty content but signaled incompleteness.
    """
    signals: set[str] = set()
    _collect_incomplete_response_signals(value, signals)
    return sorted(signals)


# ---------------------------------------------------------------------------
# Candidate resolution — TS ``resolveSummaryCandidates`` (lines 1131-1250)
# ---------------------------------------------------------------------------


def _normalize_provider_id(provider: Any) -> str:
    """Lowercase + trim provider id for stable comparison.

    Maps to TS ``normalizeProviderId`` (``summarize.ts:164-167``).
    """
    if not isinstance(provider, str):
        return ""
    return provider.strip().lower()


def _read_model_ref(value: Any) -> str:
    """Read a model-ref scalar from a string or ``{primary: ...}`` shape.

    Maps to TS ``readModelRef`` (``summarize.ts:1105-1111``). Supports
    the legacy OpenClaw config shape where ``compaction.model`` could
    be ``{primary: "claude-3-7-sonnet"}``.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        primary = value.get("primary")
        if isinstance(primary, str):
            return primary.strip()
    return ""


def _split_model_ref(model_ref: str, provider_hint: str | None) -> tuple[str, str] | None:
    """Split ``"provider/model"`` into a pair, with hint fallback.

    Lightweight Python equivalent of TS ``deps.resolveModel``. The TS
    version walks an internal model registry; here we just parse the
    ``provider/model`` slash convention and fall back to the provider
    hint. Hermes's auxiliary-client task config is the primary source
    of truth — this function only fires the slash-parse path.
    """
    if not model_ref:
        return None
    if "/" in model_ref:
        provider, _, model = model_ref.partition("/")
        provider = provider.strip()
        model = model.strip()
        if provider and model:
            return provider, model
    if provider_hint:
        return provider_hint.strip(), model_ref.strip()
    return None


def _dedupe_resolved_candidates(
    candidates: list[ResolvedSummaryCandidate],
) -> list[ResolvedSummaryCandidate]:
    """Deduplicate the candidate list on ``(provider, model)``.

    Maps to TS ``dedupeResolvedCandidates`` (``summarize.ts:1114-1128``).
    Preserves first-seen order so the 5-layer precedence (env → plugin
    → agents.compaction → agents.default → legacy) is respected.
    """
    seen: set[str] = set()
    ordered: list[ResolvedSummaryCandidate] = []
    for candidate in candidates:
        key = f"{candidate.provider}\x00{candidate.model}"
        if key in seen:
            continue
        seen.add(key)
        ordered.append(candidate)
    return ordered


def resolve_summary_candidates(
    *,
    config: Any,
    provider_hint: str | None = None,
    model_hint: str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[ResolvedSummaryCandidate]:
    """Resolve the 5-layer provider/model candidate chain.

    Verbatim port of TS ``resolveSummaryCandidates``
    (``summarize.ts:1131-1250``).

    Layers (in priority order):

    1. **Env vars** — ``LCM_SUMMARY_MODEL`` + ``LCM_SUMMARY_PROVIDER``.
    2. **Plugin config** — ``config.summary_model`` + ``config.summary_provider``.
    3. **agents.defaults.compaction.model** (legacy OpenClaw config).
    4. **agents.defaults.model** (legacy OpenClaw config).
    5. **Legacy runtime/session model** — passed in as
       ``provider_hint`` / ``model_hint`` from the engine.

    Plus appended: explicit fallback providers from
    ``config.fallback_providers``.

    Each candidate is then resolved via :func:`_split_model_ref` (slash-
    parse with provider-hint fallback) and deduped on ``(provider,
    model)``.

    ``config`` is expected to be the :class:`LcmConfig` instance from
    :mod:`lossless_hermes.db.config` (or a mapping with the same field
    names — used in tests).
    """
    if env is None:
        env = os.environ

    provider_hint = (provider_hint or "").strip() or None
    model_hint = (model_hint or "").strip() or None

    # Pull config values via getattr so dict-shaped fakes also work.
    config_summary_model = _config_get(config, "summary_model", "")
    config_summary_provider = _config_get(config, "summary_provider", "")
    fallback_providers = _config_get(config, "fallback_providers", [])

    env_provider = env.get("LCM_SUMMARY_PROVIDER", "").strip() or None
    env_model = env.get("LCM_SUMMARY_MODEL", "").strip()

    # Layer 1: env vars.
    layers: list[tuple[str, str, str | None, bool, bool]] = [
        (
            "environment variables",
            env_model,
            env_provider or provider_hint,
            bool(env_provider),
            False,
        ),
    ]
    # Layer 2: plugin config.
    plugin_provider_str = (
        config_summary_provider.strip() if isinstance(config_summary_provider, str) else ""
    )
    layers.append((
        "plugin config (lossless-hermes)",
        _read_model_ref(config_summary_model),
        plugin_provider_str or provider_hint,
        bool(plugin_provider_str),
        False,
    ))

    # Layer 3 + 4: legacy OpenClaw agents.defaults.compaction.model + agents.defaults.model.
    # The plugin config wrapper carries these through; tests can supply
    # them on the same config object via ``agents_compaction_model`` /
    # ``agents_default_model`` for parity.
    agents_compaction_model = _config_get(config, "agents_compaction_model", "")
    agents_default_model = _config_get(config, "agents_default_model", "")
    layers.append((
        "OpenClaw agents.defaults.compaction.model",
        _read_model_ref(agents_compaction_model),
        None,
        False,
        False,
    ))
    layers.append((
        "OpenClaw agents.defaults.model",
        _read_model_ref(agents_default_model),
        None,
        False,
        False,
    ))

    # Layer 5: legacy runtime/session model.
    layers.append((
        "legacy runtime/session model",
        model_hint or "",
        provider_hint,
        bool(provider_hint),
        True,  # use legacy auth profile
    ))

    resolved: list[ResolvedSummaryCandidate] = []
    for level_name, model_ref, hint, has_explicit, use_legacy in layers:
        if not model_ref:
            continue
        pair = _split_model_ref(model_ref, hint)
        if pair is None:
            if not has_explicit and "/" not in model_ref:
                logger.warning(
                    "[lcm] summaryModel %r at %r has no summaryProvider or "
                    "provider prefix. Will attempt resolution without provider.",
                    model_ref,
                    level_name,
                )
            continue
        provider, model = pair
        if provider and model:
            resolved.append(
                ResolvedSummaryCandidate(
                    level_name=level_name,
                    model=model,
                    provider=provider,
                    has_explicit_provider=has_explicit,
                    use_legacy_auth_profile=use_legacy,
                )
            )

    # Append explicit fallback providers from config.fallback_providers.
    if fallback_providers:
        for fb in fallback_providers:
            fb_provider = _config_get(fb, "provider", "")
            fb_model = _config_get(fb, "model", "")
            if not fb_provider or not fb_model:
                continue
            resolved.append(
                ResolvedSummaryCandidate(
                    level_name=f"explicit fallback ({fb_provider}/{fb_model})",
                    model=fb_model,
                    provider=fb_provider,
                    has_explicit_provider=True,
                    use_legacy_auth_profile=False,
                )
            )

    return _dedupe_resolved_candidates(resolved)


def _config_get(obj: Any, key: str, default: Any) -> Any:
    """Get a field from a pydantic model, dataclass, or mapping.

    The resolver accepts any "config-like" object — :class:`LcmConfig`,
    a dict (used in tests), a dataclass, etc. — and pulls fields via
    duck typing.
    """
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    if hasattr(obj, key):
        value = getattr(obj, key)
        if value is None:
            return default
        return value
    return default


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------


def _compute_backoff_ms(index: int) -> int:
    """Compute the exponential backoff for candidate at position ``index``.

    Verbatim port of TS ``Math.min(500 * Math.pow(2, index), 8000)``
    (``summarize.ts:1498, 1510, 1525, 1627, 1639``).

    | index | backoff (ms) |
    |---|---|
    | 0 | 500 |
    | 1 | 1000 |
    | 2 | 2000 |
    | 3 | 4000 |
    | 4 | 8000 |
    | 5+ | 8000 (capped) |
    """
    return min(500 * (2**index), 8000)


# ---------------------------------------------------------------------------
# LcmSummarizer — the cascade body
# ---------------------------------------------------------------------------


class LcmSummarizer:
    """LLM-backed summarizer with the 5-layer fallback chain.

    Verbatim port of TS ``createLcmSummarizeFromLegacyParams``
    (``summarize.ts:1258-1696``, commit ``1f07fbd``).

    Usage::

        summarizer = LcmSummarizer(
            deps=my_deps,
            config=lcm_config,
            custom_instructions="...",
            provider_hint="anthropic",
            model_hint="claude-3-7-sonnet",
        )
        summary = summarizer.summarize(text, aggressive=False)

    The summarizer exposes the ``candidates`` property so callers
    (engine, telemetry, /lcm health) can inspect the resolved chain.
    If the chain is empty, :meth:`summarize` raises :class:`RuntimeError`
    on the first call.

    Per-candidate behavior on failure (see also TS lines 1334-1685):

    * **Auth failure** → trigger ``skip_model_auth`` retry on the same
      candidate. If retry still auth-fails, throw
      :class:`LcmProviderAuthError`. The cascade catches it, logs
      ``PROVIDER FALLBACK``, applies exponential backoff, and advances
      to the next candidate.
    * **Response failure** (4xx / 5xx / finish=error) → log, backoff,
      advance.
    * **Timeout** → log, backoff, advance. On the LAST candidate's
      timeout, return :func:`build_deterministic_fallback` (NOT raise).
    * **Empty content** → envelope-aware extraction → conservative
      ``reasoning="low"`` retry → fall through to next candidate.

    **Wave-4/9 invariant** (load-bearing): on all-candidate timeout,
    the deterministic fallback ALWAYS carries the
    ``[LCM fallback summary — model unavailable; ...]`` marker. The
    :func:`build_deterministic_fallback` builder enforces this, and the
    cascade routes through it on every fallback exit.

    **Auth-short-circuit invariant** (Wave-N preserved per ADR-029): on
    all-candidate auth failure, RAISE :class:`LcmProviderAuthError`
    (do NOT return deterministic fallback). The caller catches it and
    sets ``auth_failure=True`` on :class:`CompactionResult` so the DAG
    is not corrupted by persisting a fallback summary through a
    transient provider outage.
    """

    deps: SummarizerDeps
    config: Any
    custom_instructions: str | None
    candidates: list[ResolvedSummaryCandidate]

    def __init__(
        self,
        *,
        deps: SummarizerDeps,
        config: Any,
        custom_instructions: str | None = None,
        provider_hint: str | None = None,
        model_hint: str | None = None,
        env: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Construct a summarizer for ``config`` + ``deps``.

        Args:
            deps: :class:`SummarizerDeps` implementation. The cascade
                calls ``deps.complete`` for each LLM call, ``deps.
                get_api_key`` for credential resolution, and
                ``deps.is_runtime_managed_auth_provider`` (optional)
                for the ``skip_model_auth`` gate.
            config: :class:`LcmConfig` instance (or a mapping with
                the same field names — used in tests).
            custom_instructions: Operator-supplied guidance forwarded
                to every prompt builder.
            provider_hint: Legacy/runtime provider override, used as
                layer 5 of the candidate chain.
            model_hint: Legacy/runtime model override, used as
                layer 5 of the candidate chain.
            env: Optional environment mapping (defaults to
                ``os.environ``). Tests pass a synthetic mapping to
                avoid mutating process state.
            sleep: Optional sleep callable for backoff. Defaults to
                :func:`time.sleep`. Tests pass a no-op or a mock-clock
                hook to assert backoff timing without real waits.
        """
        self.deps = deps
        self.config = config
        self.custom_instructions = custom_instructions
        self._sleep = sleep or time.sleep
        self.candidates = resolve_summary_candidates(
            config=config,
            provider_hint=provider_hint,
            model_hint=model_hint,
            env=env,
        )

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def summarize(
        self,
        text: str,
        aggressive: bool = False,
        options: LcmSummarizeOptions | None = None,
    ) -> str:
        """Summarize ``text`` and return the plain-text result.

        Verbatim port of the TS arrow function returned from
        ``createLcmSummarizeFromLegacyParams`` (``summarize.ts:1295-1686``).

        Empty/whitespace-only input short-circuits to ``""`` (matches
        the TS ``if (!text.trim()) return "";`` guard at line 1300).

        Raises:
            LcmProviderAuthError: When every candidate auth-fails (the
                auth-short-circuit invariant — see class docstring).
            RuntimeError: When the candidate chain is empty.
        """
        if not text or not text.strip():
            return ""

        if not self.candidates:
            raise RuntimeError("[lcm] createLcmSummarize: no summary model candidates resolved")

        opts = options or LcmSummarizeOptions()
        mode: SummaryMode = "aggressive" if aggressive else "normal"
        is_condensed = opts.is_condensed

        leaf_target = _config_get(self.config, "leaf_target_tokens", DEFAULT_LEAF_TARGET_TOKENS)
        condensed_target = _config_get(
            self.config, "condensed_target_tokens", DEFAULT_CONDENSED_TARGET_TOKENS
        )
        if not isinstance(leaf_target, int) or leaf_target <= 0:
            leaf_target = DEFAULT_LEAF_TARGET_TOKENS
        if not isinstance(condensed_target, int) or condensed_target <= 0:
            condensed_target = DEFAULT_CONDENSED_TARGET_TOKENS

        timeout_ms_raw = _config_get(
            self.config, "summary_timeout_ms", DEFAULT_SUMMARIZER_TIMEOUT_MS
        )
        if not isinstance(timeout_ms_raw, int) or timeout_ms_raw <= 0:
            timeout_ms = DEFAULT_SUMMARIZER_TIMEOUT_MS
        else:
            timeout_ms = timeout_ms_raw

        input_tokens = estimate_tokens(text)
        target_tokens = resolve_target_tokens(
            input_tokens=input_tokens,
            mode=mode,
            is_condensed=is_condensed,
            leaf_target_tokens=leaf_target,
            condensed_target_tokens=condensed_target,
        )

        if is_condensed:
            depth = opts.depth if opts.depth is not None else 1
            if not isinstance(depth, int) or depth < 1:
                depth = 1
            prompt = build_condensed_prompt(
                text=text,
                target_tokens=target_tokens,
                depth=depth,
                previous_summary=opts.previous_summary,
                custom_instructions=self.custom_instructions,
            )
        else:
            prompt = build_leaf_prompt(
                text=text,
                mode=mode,
                target_tokens=target_tokens,
                previous_summary=opts.previous_summary,
                custom_instructions=self.custom_instructions,
            )

        last_auth_error: LcmProviderAuthError | None = None

        for index, candidate in enumerate(self.candidates):
            provider = candidate.provider
            model = candidate.model
            next_candidate = (
                self.candidates[index + 1] if index < len(self.candidates) - 1 else None
            )

            try:
                result = self._attempt_summarizer_call(
                    candidate=candidate,
                    prompt=prompt,
                    target_tokens=target_tokens,
                    timeout_ms=timeout_ms,
                    label="initial",
                    reasoning=None,
                )
            except LcmProviderAuthError as exc:
                last_auth_error = exc
                if next_candidate:
                    logger.warning(
                        "[lcm] PROVIDER FALLBACK: %s/%s auth failed → trying %s/%s",
                        provider,
                        model,
                        next_candidate.provider,
                        next_candidate.model,
                    )
                    self._backoff(index)
                    continue
                # LCM auth-short-circuit: if all candidates auth-fail, raise rather than
                # returning deterministic fallback. Caller skips persistence to preserve
                # DAG integrity through transient provider outages.
                # Original: lossless-claw/src/summarize.ts:1665-1685 (final-throw path).
                raise last_auth_error
            except LcmProviderResponseError as exc:
                logger.warning(str(exc))
                if next_candidate:
                    logger.warning(
                        "[lcm] PROVIDER FALLBACK: %s/%s provider error → trying %s/%s",
                        provider,
                        model,
                        next_candidate.provider,
                        next_candidate.model,
                    )
                    self._backoff(index)
                    continue
                break
            except SummarizerTimeoutError as exc:
                logger.warning(
                    "[lcm] summarizer timed out; provider=%s; model=%s; timeout=%dms; error=%s",
                    provider,
                    model,
                    timeout_ms,
                    exc,
                )
                if next_candidate:
                    logger.warning(
                        "[lcm] PROVIDER FALLBACK: %s/%s timed out → trying %s/%s",
                        provider,
                        model,
                        next_candidate.provider,
                        next_candidate.model,
                    )
                    self._backoff(index)
                    continue
                logger.warning(
                    "[lcm] summarizer timed out; provider=%s; model=%s; source=fallback",
                    provider,
                    model,
                )
                return build_deterministic_fallback(text, target_tokens)
            except Exception as exc:  # noqa: BLE001 — port-verbatim
                logger.warning(
                    "[lcm] summarizer failed; provider=%s; model=%s; timeout=%dms; error=%s",
                    provider,
                    model,
                    timeout_ms,
                    exc,
                )
                if next_candidate:
                    logger.warning(
                        "[lcm] PROVIDER FALLBACK: %s/%s failed → trying %s/%s",
                        provider,
                        model,
                        next_candidate.provider,
                        next_candidate.model,
                    )
                    self._backoff(index)
                    continue
                break

            # Normalize the response content.
            content = result.get("content") if isinstance(result, Mapping) else None
            summary, block_types = normalize_completion_summary(content)
            summary_source: Literal["content", "envelope", "retry", "fallback"] = "content"

            # --- Empty-summary hardening: envelope → retry → deterministic ---
            if not summary:
                envelope_summary, envelope_types = normalize_completion_summary(result)
                if envelope_summary:
                    summary = envelope_summary
                    summary_source = "envelope"
                    logger.info(
                        "[lcm] recovered summary from response envelope; "
                        "provider=%s; model=%s; block_types=%s; source=envelope",
                        provider,
                        model,
                        ",".join(envelope_types) if envelope_types else "(none)",
                    )

            incomplete_signals = extract_incomplete_response_signals(result)
            initial_summary = summary
            should_retry_incomplete = bool(summary) and bool(incomplete_signals)

            if not summary or should_retry_incomplete:
                logger.warning(
                    "[lcm] %s on first attempt; provider=%s; model=%s; "
                    "block_types=%s; retrying with conservative settings",
                    (
                        "incomplete summary response"
                        if should_retry_incomplete
                        else "empty normalized summary"
                    ),
                    provider,
                    model,
                    ",".join(block_types) if block_types else "(none)",
                )

                try:
                    retry_result = self._attempt_summarizer_call(
                        candidate=candidate,
                        prompt=prompt,
                        target_tokens=target_tokens,
                        timeout_ms=timeout_ms,
                        label="retry",
                        reasoning="low",
                    )
                    retry_content = (
                        retry_result.get("content") if isinstance(retry_result, Mapping) else None
                    )
                    retry_summary, retry_types = normalize_completion_summary(retry_content)
                    if not retry_summary:
                        retry_summary, retry_types = normalize_completion_summary(retry_result)
                    summary = retry_summary

                    if summary:
                        summary_source = "retry"
                        logger.info(
                            "[lcm] retry succeeded; provider=%s; model=%s; "
                            "block_types=%s; source=retry",
                            provider,
                            model,
                            ",".join(retry_types) if retry_types else "(none)",
                        )
                    else:
                        if next_candidate:
                            logger.warning(
                                "[lcm] retry also returned empty summary; "
                                "provider=%s; model=%s; retrying with %s/%s",
                                provider,
                                model,
                                next_candidate.provider,
                                next_candidate.model,
                            )
                            self._backoff(index)
                            continue
                        logger.warning(
                            "[lcm] retry also returned empty summary; "
                            "provider=%s; model=%s; falling back to truncation",
                            provider,
                            model,
                        )
                        summary = initial_summary
                except LcmProviderAuthError as exc:
                    last_auth_error = exc
                    if next_candidate:
                        logger.warning(
                            "[lcm] PROVIDER FALLBACK: %s/%s auth failed on retry → trying %s/%s",
                            provider,
                            model,
                            next_candidate.provider,
                            next_candidate.model,
                        )
                        self._backoff(index)
                        continue
                    raise last_auth_error
                except LcmProviderResponseError as exc:
                    logger.warning(str(exc))
                    if next_candidate:
                        logger.warning(
                            "[lcm] PROVIDER FALLBACK: %s/%s provider error on retry → trying %s/%s",
                            provider,
                            model,
                            next_candidate.provider,
                            next_candidate.model,
                        )
                        self._backoff(index)
                        continue
                    summary = initial_summary
                    continue
                except SummarizerTimeoutError as exc:
                    if next_candidate:
                        logger.warning(
                            "[lcm] retry timed out; provider=%s; model=%s; "
                            "timeout=%dms; error=%s; retrying with %s/%s",
                            provider,
                            model,
                            timeout_ms,
                            exc,
                            next_candidate.provider,
                            next_candidate.model,
                        )
                        self._backoff(index)
                        continue
                    logger.warning(
                        "[lcm] retry timed out; provider=%s; model=%s; "
                        "timeout=%dms; error=%s; falling back to truncation",
                        provider,
                        model,
                        timeout_ms,
                        exc,
                    )
                    summary = initial_summary
                except Exception as exc:  # noqa: BLE001
                    if next_candidate:
                        logger.warning(
                            "[lcm] retry failed; provider=%s; model=%s; "
                            "timeout=%dms; error=%s; retrying with %s/%s",
                            provider,
                            model,
                            timeout_ms,
                            exc,
                            next_candidate.provider,
                            next_candidate.model,
                        )
                        continue
                    logger.warning(
                        "[lcm] retry failed; provider=%s; model=%s; "
                        "timeout=%dms; error=%s; falling back to truncation",
                        provider,
                        model,
                        timeout_ms,
                        exc,
                    )
                    summary = initial_summary

            if not summary:
                summary_source = "fallback"
                logger.error(
                    "[lcm] all extraction attempts exhausted; provider=%s; "
                    "model=%s; source=fallback",
                    provider,
                    model,
                )
                return build_deterministic_fallback(text, target_tokens)

            if summary_source != "content":
                logger.info(
                    "[lcm] summary resolved via non-content path; provider=%s; model=%s; source=%s",
                    provider,
                    model,
                    summary_source,
                )

            return summary

        # All candidates exhausted without success.
        logger.error(
            "[lcm] ALL PROVIDERS EXHAUSTED: %d candidate(s) tried, none succeeded. "
            "Compaction falling back to deterministic truncation. "
            "Check provider keys and quotas.",
            len(self.candidates),
        )
        if last_auth_error is not None:
            # LCM auth-short-circuit: if all candidates auth-fail, raise rather than
            # returning deterministic fallback. Caller skips persistence to preserve
            # DAG integrity through transient provider outages.
            # Original: lossless-claw/src/summarize.ts:1665-1685 (final-throw path).
            raise last_auth_error
        return build_deterministic_fallback(text, target_tokens)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _backoff(self, index: int) -> None:
        """Apply ``min(500 * 2^index, 8000)`` ms backoff before next candidate.

        The cap at 8000ms protects the candidate chain from runaway
        backoff growth — the 6th and later candidates use the same
        8s wait. Calls :attr:`_sleep` (a constructor-injected hook) so
        tests can assert timing without burning wall-clock time.
        """
        backoff_ms = _compute_backoff_ms(index)
        self._sleep(backoff_ms / 1000.0)

    def _run_summarizer_call(
        self,
        *,
        candidate: ResolvedSummaryCandidate,
        prompt: str,
        target_tokens: int,
        api_key: str | None,
        timeout_ms: int,
        label: str,
        reasoning: str | None,
        skip_model_auth: bool,
    ) -> Mapping[str, Any]:
        """Invoke ``deps.complete`` wrapped in :func:`_with_timeout`.

        Maps to TS ``runSummarizerCall`` (``summarize.ts:1347-1372``).

        The ``reasoning`` parameter is forwarded as a kwarg to
        ``deps.complete``. Per the porting-guide remaining-5%-risk #4,
        Hermes's ``auxiliary_client.call_llm`` does NOT have a
        ``reasoning`` parameter; the dep implementation is responsible
        for either piping it through ``extra_body={"reasoning_effort":
        ...}`` (OpenAI-style) or accepting that the conservative retry
        just retries with the same settings.
        """

        def call() -> Mapping[str, Any]:
            return self.deps.complete(
                provider=candidate.provider,
                model=candidate.model,
                api_key=api_key,
                system=LCM_SUMMARIZER_SYSTEM_PROMPT,
                user_prompt=prompt,
                max_tokens=target_tokens,
                reasoning=reasoning,
                skip_model_auth=skip_model_auth,
                timeout_ms=timeout_ms,
            )

        return _with_timeout(call, timeout_ms=timeout_ms, label=label)

    def _retry_without_model_auth(
        self,
        *,
        candidate: ResolvedSummaryCandidate,
        prompt: str,
        target_tokens: int,
        timeout_ms: int,
        failure: ProviderAuthFailure,
        reasoning: str | None,
    ) -> Mapping[str, Any]:
        """Retry the call with ``skip_model_auth=True``.

        Maps to TS ``retryWithoutModelAuth`` (``summarize.ts:1374-1449``).

        Behavior:

        * Skipped (raises the initial auth error) when
          ``deps.is_runtime_managed_auth_provider(provider)`` is True —
          OAuth-managed providers cannot use the bypass.
        * Otherwise: fetches direct credentials via
          ``deps.get_api_key(skip_model_auth=True)``. If none found,
          raises the initial auth error.
        * On retry: a final auth-failure check uses
          ``require_structural_signal=True`` (the success-path
          discipline — the summary text may legitimately mention
          "auth error").
        """
        provider = candidate.provider
        model = candidate.model
        initial_auth_error = LcmProviderAuthError(provider=provider, model=model, failure=failure)

        is_runtime_managed = False
        if hasattr(self.deps, "is_runtime_managed_auth_provider"):
            try:
                is_runtime_managed = bool(self.deps.is_runtime_managed_auth_provider(provider))
            except Exception:  # noqa: BLE001
                # Hook is best-effort; treat exceptions as "not runtime-managed".
                is_runtime_managed = False
        if is_runtime_managed:
            raise initial_auth_error

        logger.warning(str(initial_auth_error))
        logger.warning(
            "[lcm] summarizer auth retry: retrying %s/%s without runtime.modelAuth credentials.",
            provider,
            model,
        )

        direct_api_key = self.deps.get_api_key(provider, model, skip_model_auth=True)
        if not direct_api_key:
            logger.warning(
                "[lcm] summarizer auth retry unavailable: no direct credentials found for %s/%s.",
                provider,
                model,
            )
            raise initial_auth_error

        try:
            direct_result = self._run_summarizer_call(
                candidate=candidate,
                prompt=prompt,
                target_tokens=target_tokens,
                api_key=direct_api_key,
                timeout_ms=timeout_ms,
                label="auth-retry",
                reasoning=reasoning,
                skip_model_auth=True,
            )
        except (LcmProviderAuthError, LcmProviderResponseError):
            raise
        except Exception as direct_err:
            # Catch path: real errors carry structural signals (HTTP 401,
            # error.kind), so requireStructuralSignal is safe here too.
            direct_failure = extract_provider_auth_failure(
                direct_err, require_structural_signal=True
            )
            if direct_failure is not None:
                retry_auth_error = LcmProviderAuthError(
                    provider=provider, model=model, failure=direct_failure
                )
                logger.warning(str(retry_auth_error))
                raise retry_auth_error from direct_err
            raise

        # Success-path failure detection on the retry result.
        direct_failure = extract_provider_auth_failure(
            direct_result, require_structural_signal=True
        )
        if direct_failure is not None:
            retry_auth_error = LcmProviderAuthError(
                provider=provider, model=model, failure=direct_failure
            )
            logger.warning(str(retry_auth_error))
            raise retry_auth_error

        direct_response_failure = extract_provider_response_failure(direct_result)
        if direct_response_failure is not None:
            raise LcmProviderResponseError(
                provider=provider, model=model, failure=direct_response_failure
            )

        logger.info(
            "[lcm] summarizer auth retry succeeded; provider=%s; model=%s; "
            "source=direct-credentials",
            provider,
            model,
        )
        return direct_result

    def _attempt_summarizer_call(
        self,
        *,
        candidate: ResolvedSummaryCandidate,
        prompt: str,
        target_tokens: int,
        timeout_ms: int,
        label: str,
        reasoning: str | None,
    ) -> Mapping[str, Any]:
        """One end-to-end LLM call against ``candidate`` with auth-retry plumbing.

        Maps to TS ``attemptSummarizerCall`` (``summarize.ts:1451-1486``).

        Returns the provider envelope on success. Raises:

        * :class:`LcmProviderAuthError` — auth failure (after retry path
          exhausted).
        * :class:`LcmProviderResponseError` — non-auth response failure.
        * :class:`SummarizerTimeoutError` — per-call timeout.
        * Other exceptions are re-raised verbatim.
        """
        api_key = self.deps.get_api_key(candidate.provider, candidate.model)
        try:
            result = self._run_summarizer_call(
                candidate=candidate,
                prompt=prompt,
                target_tokens=target_tokens,
                api_key=api_key,
                timeout_ms=timeout_ms,
                label=label,
                reasoning=reasoning,
                skip_model_auth=False,
            )
        except LcmProviderResponseError:
            raise
        except SummarizerTimeoutError:
            raise
        except Exception as exc:
            # Caught-error path: use the broad pattern detection.
            auth_failure = extract_provider_auth_failure(exc)
            if auth_failure is None:
                raise
            return self._retry_without_model_auth(
                candidate=candidate,
                prompt=prompt,
                target_tokens=target_tokens,
                timeout_ms=timeout_ms,
                failure=auth_failure,
                reasoning=reasoning,
            )

        # Success-path: use the structural-signal-only discipline so that
        # legitimate summary text containing "auth error" phrases is NOT
        # mistaken for an actual API failure.
        auth_failure = extract_provider_auth_failure(result, require_structural_signal=True)
        if auth_failure is None:
            response_failure = extract_provider_response_failure(result)
            if response_failure is None:
                return result
            raise LcmProviderResponseError(
                provider=candidate.provider,
                model=candidate.model,
                failure=response_failure,
            )
        return self._retry_without_model_auth(
            candidate=candidate,
            prompt=prompt,
            target_tokens=target_tokens,
            timeout_ms=timeout_ms,
            failure=auth_failure,
            reasoning=reasoning,
        )
