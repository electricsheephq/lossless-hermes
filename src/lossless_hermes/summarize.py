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

from typing import Final, Literal

__all__ = [
    "LCM_SUMMARIZER_SYSTEM_PROMPT",
    "SummaryMode",
    "build_condensed_prompt",
    "build_deterministic_fallback",
    "build_leaf_prompt",
]


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
