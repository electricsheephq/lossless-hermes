"""LCM recall-policy prompt text + user-voice rewording (ADR-014).

The ``LOSSLESS_RECALL_POLICY_PROMPT`` constant is the ~3 KB text the
plugin injects into every turn's user-message content via
``pre_llm_call`` (issue 03-10). The text guides the agent on when to
reach for ``lcm_grep`` / ``lcm_describe`` / ``lcm_expand_query`` and how
to interpret compacted summaries.

### Two-layer design (ADR-014 §Rationale)

The TS source (``lossless-claw/src/plugin/index.ts:244-321``, commit
``1f07fbd``) injects this same text into the **system prompt** via
``before_prompt_build`` → ``prependSystemContext``. Hermes deliberately
diverges: ``pre_llm_call`` return values are appended to the **user
message** (not system prompt) to preserve the Anthropic prompt cache
(``hermes_cli/plugins.py:invoke_hook`` docstring — "Context is ALWAYS
injected into the user message, never the system prompt").

Per ADR-014 §Decision the TS text needs minor rewording so it reads
naturally as a user-message preamble rather than a system instruction.
Two constants live here so reviewers can diff each layer:

* :data:`_RAW_POLICY_PROMPT` — verbatim port of the TS string-array
  joined with ``"\\n"`` (``lossless-claw/src/plugin/index.ts:245-320``).
  Reviewers can diff line-for-line against TS to confirm no semantic
  content was lost in transit.
* :data:`LOSSLESS_RECALL_POLICY_PROMPT` — the output of
  :func:`reword_for_user_voice` applied to the raw text. This is what
  the ``pre_llm_call`` hook returns.

### The rewording (semantic preservation)

The TS text is already mostly 3rd-person advisory ("Prefer X",
"Use Y when Z"), so the user-voice transformation is a small,
auditable set of substitutions documented in
:data:`_USER_VOICE_REPLACEMENTS`. Each entry is a (find, replace) pair
with a rationale comment. The set is:

1. Project name: ``lossless-claw`` → ``lossless-hermes`` — the Python
   port is a separate distribution; the policy text must reference the
   correct package name.
2. Activation phrase rewording: the system-voice opener
   ``"The lossless-claw plugin is active."`` becomes a user-voice
   preamble that frames the rest as available capability, not a
   directive.
3. Title softening: ``## Lossless Recall Policy`` →
   ``## Lossless Recall Guidance`` — "Policy" reads as system-issued
   rule-set; "Guidance" reads naturally as user-supplied context.

That's the full diff. Every behavioral instruction (the
escalation order, the tool-routing rules, the precision flow, the
uncertainty checklist) is preserved verbatim — only the activation
framing and naming shift.

### Why a function instead of just rewriting the constant

A programmatic transformation makes the diff between system-voice and
user-voice trivially auditable: reviewers can read
:data:`_USER_VOICE_REPLACEMENTS` to see the exact set of changes and
verify each one preserves the underlying behavior. A snapshot test
(``tests/test_recall_policy.py``) asserts the rewording is exact so
future edits cannot silently introduce semantic drift.

See:

* ``docs/adr/014-recall-policy-injection.md`` — Option A decision +
  rewording rationale.
* ``docs/adr/010-always-on-assembly-emulation.md`` — ``pre_llm_call``
  as the always-on substitution seam.
* ``lossless-claw/src/plugin/index.ts:244-321`` — TS source pinned to
  commit ``1f07fbd``.
* ``epics/03-ingest-assembly/03-10-recall-policy-injection.md`` — this
  issue's spec and acceptance criteria.
"""

from __future__ import annotations

from typing import Final, Tuple

__all__ = [
    "LOSSLESS_RECALL_POLICY_PROMPT",
    "reword_for_user_voice",
]


# ---------------------------------------------------------------------------
# _RAW_POLICY_PROMPT — verbatim port of TS constant
# ---------------------------------------------------------------------------
#
# Source: ``lossless-claw/src/plugin/index.ts:244-321`` (commit ``1f07fbd``).
# The TS form is an array of strings joined with ``"\n"``. We join the same
# way so the resulting text is byte-identical to what the TS bridge would
# have injected into a system prompt. The user-voice rewording (below)
# transforms this to user-message form per ADR-014.
#
# DO NOT EDIT this constant without updating the TS pin AND running the
# snapshot test in ``tests/test_recall_policy.py`` — it asserts a stable
# hash so silent drift surfaces as a test failure.

_RAW_POLICY_LINES: Final[Tuple[str, ...]] = (
    "## Lossless Recall Policy",
    "",
    "The lossless-claw plugin is active.",
    "",
    "For compacted conversation history, these instructions supersede generic memory-recall guidance. Prefer lossless-claw recall tools first when answering questions about prior conversation content, decisions made in the conversation, or details that may have been compacted.",
    "",
    "**Conflict handling:** If newer evidence conflicts with an older summary or recollection, prefer the newer evidence. Do not trust a stale summary over fresher contradictory information.",
    "",
    "**Contradictions/uncertainty:** If facts seem contradictory or uncertain, verify with lossless-claw recall tools before answering instead of trusting the summary at face value.",
    "",
    "**Tool escalation:**",
    "Recall order for compacted conversation history:",
    "1. `lcm_grep` — search by regex or full-text across messages and summaries",
    "2. `lcm_describe` — inspect a specific summary (cheap, no sub-agent)",
    "3. `lcm_expand_query` — deep recall: spawns bounded sub-agent, expands DAG, and returns answer plus cited summary IDs in tool output for follow-up (~120s, don't ration it)",
    "",
    "**Specialized tools beyond the 1/2/3 escalation** (use when the question type clearly matches):",
    '- **Time-anchored** ("what did we work on yesterday/last week?"): `lcm_synthesize_around` with `window_kind="period"` and a period shortcut (`yesterday`, `last-7-days`, `this-month`, `last-12h`, etc.) OR explicit `since`/`before`. No anchor lookup needed.',
    '- **Topic-anchored / paraphrastic** ("did we discuss X?"): `lcm_grep mode="hybrid"` (FTS + Voyage rerank — strongest recall) or `lcm_grep mode="semantic"` (embedding-only, cheaper, with confidence band; supports `summaryKinds` filter for kind-scoped recall).',
    '- **Verbatim citation** ("quote exactly what was said"): `lcm_grep mode="verbatim"` returns FULL untruncated message rows with optional `role` filter (user/assistant/tool/system).',
    '- **Entity / pattern** ("who is this person?", "history of project X"): `lcm_get_entity` (exact name) or `lcm_search_entities` (fuzzy). Entity catalog is populated by an async worker; if empty, the tools return a `catalogStatus` field.',
    '- **Drilldown** ("where did this come from?"): `lcm_describe` with `expandChildren=true` or `expandMessages=true` for inline one-hop expansion (no sub-agent). For deeper traversal, `lcm_expand_query`.',
    "",
    "**`lcm_grep` routing guidance:**",
    '- Prefer `mode: "full_text"` for keyword or topical recall; keep `mode: "regex"` for literal patterns.',
    '- For paraphrastic / topical recall ("did we discuss X?"), `mode: "hybrid"` (FTS + Voyage rerank — best recall) or `mode: "semantic"` (embedding only — cheaper).',
    '- For citation / "exactly what was said", `mode: "verbatim"` returns full untruncated message rows. Combine with `role: "user"|"assistant"|"tool"|"system"` to filter.',
    "- Full-text queries use FTS5 semantics, and FTS5 defaults to AND matching, so extra terms make matching stricter rather than broader.",
    "- Prefer 1-3 distinctive full-text terms or one quoted phrase. Do not pad queries with synonyms or extra keywords.",
    '- Wrap exact multi-word phrases in quotes, for example `"error handling"`.',
    '- Keep the default `sort: "recency"` for "what just happened?" lookups.',
    '- Use `sort: "relevance"` when hunting for the best older match on a topic.',
    '- Use `sort: "hybrid"` when relevance matters but newer context should still get a boost.',
    "",
    "**`lcm_expand_query` usage** — two patterns (always requires `prompt`):",
    '- With IDs: `lcm_expand_query(summaryIds: ["sum_xxx"], prompt: "What config changes were discussed?")`',
    '- With search: `lcm_expand_query(query: "database migration", prompt: "What strategy was decided?")`',
    "- `query` uses the same FTS5 full-text search path as `lcm_grep`, so the same query-construction rules apply.",
    "- `query` is for matching candidate summaries; `prompt` is the natural-language question or task to answer after expansion.",
    "- FTS5 defaults to AND matching, so more query terms narrow results instead of broadening them.",
    "- For `query`, use 1-3 distinctive terms or a quoted phrase. Do not stuff synonyms or extra keywords into it.",
    "**Scope selection rule:**",
    "- Start with the current conversation scope.",
    "- If the in-context summaries already look relevant to the user's question, prefer `lcm_grep` or `lcm_expand_query` without `allConversations`.",
    "- Use `allConversations: true` only when the current summaries do not appear sufficient, the question seems outside the current conversation, or the user is explicitly asking about work across sessions.",
    "- For global discovery, prefer `lcm_grep(..., allConversations: true)` first.",
    "- If global matches are found and the user needs one synthesized answer, use `lcm_expand_query(..., allConversations: true)`; this is bounded synthesis, not exhaustive expansion.",
    "- If you already know the exact target conversation, prefer explicit `conversationId` instead of `allConversations`.",
    "- Optional: `maxTokens` (default 2000), `conversationId`, `allConversations: true`",
    "- Keep raw summary IDs out of normal user-facing prose unless the user explicitly asks for sources or IDs.",
    "",
    "## Compacted Conversation Context",
    "",
    "If compacted summaries appear above, treat them as compressed recall cues rather than proof of exact wording or exact values.",
    "",
    'If a summary includes an "Expand for details about:" footer, use it as a cue to expand before asserting specifics.',
    "",
    "For exact commands, SHAs, paths, timestamps, config values, or causal chains, expand for details before answering.",
    "",
    "State uncertainty instead of guessing from compacted summaries.",
    "",
    "**Precision flow:**",
    "1. `lcm_grep` to find the relevant summaries or messages",
    "2. `lcm_expand_query` when you need exact evidence before answering",
    "3. Answer from the retrieved evidence instead of summary paraphrase",
    "",
    "**Uncertainty checklist:**",
    "- Am I making an exact factual claim from compacted context?",
    "- Could compaction have omitted a crucial detail?",
    "- Would I need an expansion step if the user asks for proof or exact text?",
    "",
    "If yes to any item, expand first or explicitly say that you need to expand.",
    "",
    "These precedence rules apply only to compacted conversation history. Lossless-claw does not supersede memory tools globally.",
    "",
    "If a summary conflicts with newer evidence, prefer the newer evidence. Do not guess exact commands, SHAs, paths, timestamps, config values, or causal claims from compacted summaries when expansion is needed.",
)

_RAW_POLICY_PROMPT: Final[str] = "\n".join(_RAW_POLICY_LINES)


# ---------------------------------------------------------------------------
# _USER_VOICE_REPLACEMENTS — the audit-friendly rewording table
# ---------------------------------------------------------------------------
#
# Per ADR-014 §Decision the TS text needs minor voice adjustment to read
# naturally as a user-message preamble rather than a system instruction.
# Every behavioral instruction must survive — only framing changes.
#
# Each tuple is ``(find, replace)`` applied via :meth:`str.replace`. The
# tuples are applied in order; downstream tuples may rely on upstream ones
# (e.g. the title rewording assumes the project-name swap has run).

_USER_VOICE_REPLACEMENTS: Final[Tuple[Tuple[str, str], ...]] = (
    # 1. Project-name swap. The Python port is the ``lossless-hermes``
    #    distribution; every mention of the TS package name shifts to the
    #    Python one so the policy text is internally consistent.
    ("lossless-claw", "lossless-hermes"),
    ("Lossless-claw", "Lossless-hermes"),
    # 2. Activation-phrase reword. The TS opener "The lossless-claw plugin
    #    is active." reads as a system-prompt status line. In a user
    #    message it reads more naturally as user-supplied framing for the
    #    rest of the section.
    (
        "The lossless-hermes plugin is active.",
        "The lossless-hermes recall tools are available for this conversation; the guidance below describes how to use them.",
    ),
    # 3. Title softening. "Policy" reads as a system-issued rule-set; in a
    #    user message it reads more naturally as advisory guidance.
    (
        "## Lossless Recall Policy",
        "## Lossless Recall Guidance",
    ),
)


def reword_for_user_voice(text: str) -> str:
    """Apply the user-voice replacement table to ``text``.

    Per ADR-014 §Decision, the TS recall-policy prompt is written for
    the system-prompt position and needs a small set of framing changes
    to read naturally as user-message content. This function is the
    documented, testable transformation.

    The replacements (see :data:`_USER_VOICE_REPLACEMENTS`):

    1. ``lossless-claw`` → ``lossless-hermes`` — Python port naming.
    2. The "plugin is active" activation phrase becomes user-voice
       framing.
    3. Section title "Policy" → "Guidance" — softer in user voice.

    Every behavioral instruction (escalation order, tool-routing rules,
    precision flow, uncertainty checklist) is preserved verbatim.

    Idempotent: calling ``reword_for_user_voice(reword_for_user_voice(t))``
    yields the same result as ``reword_for_user_voice(t)`` because each
    replacement pair has a ``replace`` clause that does not contain the
    ``find`` clause.

    Args:
        text: The system-voice policy text (typically
            :data:`_RAW_POLICY_PROMPT`).

    Returns:
        The user-voice version of ``text``.
    """
    out = text
    for find, replace in _USER_VOICE_REPLACEMENTS:
        out = out.replace(find, replace)
    return out


# ---------------------------------------------------------------------------
# LOSSLESS_RECALL_POLICY_PROMPT — the public, user-voice text
# ---------------------------------------------------------------------------
#
# This is what :meth:`_AssembleMixin._on_pre_llm_call` returns inside the
# ``{"context": ...}`` dict per ADR-014. The constant is computed at
# import time so the rewording runs exactly once per process.

LOSSLESS_RECALL_POLICY_PROMPT: Final[str] = reword_for_user_voice(_RAW_POLICY_PROMPT)
