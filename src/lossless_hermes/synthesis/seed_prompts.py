"""Seed the default synthesis prompts (issue 07-08).

Idempotently seeds the 10–11 §12 default prompt rows into
:sql:`lcm_prompt_registry`. Without this seed, :func:`dispatch_synthesis`
(issue 07-05) and ``lcm_synthesize_around`` (cycle-2) would return
``missing_prompt`` errors on every call because the registry would be
empty.

### Idempotency

For every default row, the seed function first asks: "does any row
already exist for this ``(memory_type, tier_label, pass_kind)`` triple
— active OR archived?". If yes, skip; if no, insert v1. Operator
overrides are NEVER clobbered.

### No nested BEGIN

The seed is invoked from ``_seed_default_prompts`` inside the migration
ladder, which is itself wrapped in ``BEGIN EXCLUSIVE``. So the function
uses **raw INSERT** (NOT :func:`register_prompt`) — opening another
``BEGIN IMMEDIATE`` inside ``BEGIN EXCLUSIVE`` would fail with
``"cannot start a transaction within a transaction"``. The seed is
designed to compose with any outer transaction.

### Wave-9 P1 placeholder hygiene

# LCM Wave-9 (2026-03-08): no ``{{date_range}}`` or ``{{target_length}}``
# placeholders. ``render_prompt`` (in dispatch, issue 07-05) does not
# substitute those, so they would ship verbatim to the LLM. Same class
# as Final.review.3 Loop 4 Bug 4.2. Each template has an inline comment
# at the top stating the Wave-9 invariant; tests assert the templates
# contain neither token.

### Source pin

* TS canonical: ``lossless-claw/src/synthesis/seed-default-prompts.ts``
  (commit ``1f07fbd`` on branch ``pr-613``, 435 LOC).
* Spec: ``epics/07-entity-synthesis/07-08-prompt-registry.md``.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass

from lossless_hermes.synthesis.types import MemoryType, PassKind

__all__ = ["DEFAULT_PROMPTS", "SeedPromptDef", "SeedResult", "seed_default_prompts"]


@dataclass(frozen=True, slots=True)
class SeedPromptDef:
    """A single default-prompt row definition.

    Mirrors the TS ``SeedPromptDef`` interface in
    ``seed-default-prompts.ts:26-33``. ``model_recommendation`` defaults
    to ``None`` (TS: optional); ``notes`` defaults to ``"v4.1 §12 default seed"``
    matching the TS fallback in :func:`seed_default_prompts`.
    """

    memory_type: MemoryType
    tier_label: str | None
    pass_kind: PassKind
    template: str
    model_recommendation: str | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class SeedResult:
    """Outcome of a :func:`seed_default_prompts` call.

    * ``seeded`` — number of triples newly inserted in this call.
    * ``skipped`` — number of triples that already had a row (operator
      override or prior seed run).

    Sum is always equal to the static size of :data:`DEFAULT_PROMPTS`.
    """

    seeded: int
    skipped: int


# ---------------------------------------------------------------------------
# Default prompts — byte-for-byte port of ``DEFAULT_PROMPTS`` in
# ``lossless-claw/src/synthesis/seed-default-prompts.ts:59-369``.
# ---------------------------------------------------------------------------
#
# The 11 default prompts from architecture-v4.1.md §12 (Appendix A).
#
# Conventions (mirrors TS docstring at seed-default-prompts.ts:36-58):
#   - ``{{source_text}}`` substitution placeholder used by dispatch_synthesis
#     for the source bundle (leaves concatenated, or condensed-summary
#     bundle, depending on tier).
#   - ``{{tier}}`` substitutes the tier name (daily / weekly / monthly / etc).
#   - ``{{memory_type}}`` substitutes the memory type.
#   - ``{{draft}}``, ``{{candidate_summary}}``, ``{{candidates}}``, and
#     ``{{source_leaves}}`` are used by the verify-fidelity + best-of-N
#     judge passes.
#
# LCM Wave-9 (2026-03-08): no ``{{date_range}}`` or ``{{target_length}}``
# placeholders — same class as Final.review.3 Loop 4 Bug 4.2. Verified
# by ``test_seeded_templates_no_forbidden_placeholders`` (issue 07-08).

DEFAULT_PROMPTS: tuple[SeedPromptDef, ...] = (
    # LCM Wave-9 (2026-03-08): episodic-leaf — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="episodic-leaf",
        tier_label=None,
        pass_kind="single",
        template="""You are a meticulous summarizer for a lossless memory system.

Summarize the following messages from a conversation. Capture:
- All decisions made or reversed
- Concrete actions taken (with file paths, commit SHAs, PR numbers when present)
- Open questions and blockers
- Entities mentioned (people, projects, tools, concepts)
- Time markers (when relevant)

Style:
- Compact but complete. No filler.
- Use original terminology — do not rename entities or paraphrase technical terms.
- Bullet structure where useful; prose where bullets would over-fragment.
- Include any verbatim quotes that preserve key intent.

Length: target 800-1500 tokens. Hard cap 4000 tokens.

CONVERSATION:
{{source_text}}

SUMMARY:""",
        notes="v4.1 §12 default — episodic leaf summarizer. Override with operator runtime if customized.",
    ),
    # LCM Wave-9 (2026-03-08): episodic-condensed/daily — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="episodic-condensed",
        tier_label="daily",
        pass_kind="single",
        template="""You are a meticulous summarizer condensing leaf-level summaries into a daily summary.

Input is N leaf summaries from a single day. Produce a daily summary that:
- Preserves every distinct decision (reference original leaf IDs in citations)
- Preserves every concrete action (file paths, PRs, commits) — DO NOT abstract these away
- Identifies recurring themes and patterns
- Notes any contradictions across leaves (if leaf A says X then leaf B says Y, surface both)
- Preserves Eva's actual phrasing where it captures nuance

Citations: include source leaf IDs in [bracket] notation after each major claim.

Length: target 1500-2500 tokens.

LEAF SUMMARIES:
{{source_text}}

DAILY SUMMARY:""",
        notes="v4.1 §12 default — daily condensed.",
    ),
    # LCM Wave-9 (2026-03-08): episodic-condensed/weekly — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="episodic-condensed",
        tier_label="weekly",
        pass_kind="single",
        template="""You are a meticulous summarizer condensing daily summaries into a weekly summary.

Input is 7 (or fewer) daily summaries from a single week. Produce a weekly summary that:
- Preserves every distinct decision (reference original daily IDs in citations)
- Preserves every concrete action (file paths, PRs, commits) — DO NOT abstract these away
- Identifies recurring themes and patterns
- Notes any contradictions across days
- Preserves Eva's actual phrasing where it captures nuance

Citations: include source daily IDs in [bracket] notation after each major claim.

Length: target 2500-4000 tokens.

DAILY SUMMARIES:
{{source_text}}

WEEKLY SUMMARY:""",
        notes="v4.1 §12 default — weekly condensed.",
    ),
    # LCM Wave-9 (2026-03-08): episodic-condensed/monthly — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="episodic-condensed",
        tier_label="monthly",
        pass_kind="single",
        template="""You are a meticulous summarizer condensing weekly summaries into a monthly summary.

Input is 4-5 weekly summaries from a single month. Produce a monthly summary that:
- Preserves every distinct decision (reference original weekly IDs in citations)
- Preserves every concrete action (file paths, PRs, commits) — DO NOT abstract these away
- Identifies the month's overarching themes (3-5 max)
- Notes any contradictions across weeks
- Preserves Eva's actual phrasing where it captures nuance

Citations: include source weekly IDs in [bracket] notation after each major claim.

Length: target 4000-6000 tokens.

WEEKLY SUMMARIES:
{{source_text}}

MONTHLY SUMMARY:""",
        notes="v4.1 §12 default — monthly condensed (followed by verify_fidelity pass).",
    ),
    # LCM Wave-9 (2026-03-08): episodic-condensed/monthly verify_fidelity — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="episodic-condensed",
        tier_label="monthly",
        pass_kind="verify_fidelity",
        template="""You are a fidelity checker. The DRAFT summary below was condensed from SOURCE leaves.
Your ONLY job: identify any claim in the DRAFT not supported by the SOURCE.

DO NOT:
- Suggest things that are "missing" — that's not your job
- Suggest improvements to phrasing or completeness
- Add new content

DO:
- Extract each factual claim from the DRAFT
- For each claim: cite the SOURCE passage that supports it (if any)
- For unsupported claims: list them as `UNSUPPORTED: <claim>`

If all claims are supported: respond `OK: all <N> claims grounded`.

DRAFT:
{{draft}}

SOURCE:
{{source_leaves}}

FIDELITY REPORT:""",
        notes="v4.1 §12 default — monthly verify_fidelity (catches hallucinations only, NOT a critique-revise).",
    ),
    # LCM Wave-9 (2026-03-08): episodic-yearly/single — no {{date_range}} or {{target_length}}.
    # Note: tier_label='yearly' is used by dispatch_synthesis for tier=yearly;
    # memory_type='episodic-yearly' (not 'episodic-condensed') matches §12.
    SeedPromptDef(
        memory_type="episodic-yearly",
        tier_label="yearly",
        pass_kind="single",
        template="""You are synthesizing a YEAR of memory into a single durable summary that will be read for years to come.

Input: 12 monthly condensed summaries spanning the year.

Your output is one synthesis. We will generate 3 such syntheses in parallel (different random seeds) and a separate judge will pick the best. So: synthesize boldly, prioritize narrative coherence, do not hedge.

Capture:
- The year's overarching themes (3-5 max)
- Major decisions and their rationale
- Major shifts in approach (what we tried, what worked, what we abandoned)
- Recurring people and their roles (Eva, Andrew, key collaborators)
- Concrete artifacts produced (PRs, projects, repos)
- The year's "shape" — was it growth, recovery, exploration, scaling?

Length: target 5000-8000 tokens.

MONTHLIES:
{{source_text}}

YEAR SYNTHESIS:""",
        notes="v4.1 §12 default — yearly single-candidate (one of 3 in best_of_n).",
    ),
    # LCM Wave-9 (2026-03-08): episodic-yearly/best_of_n_judge — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="episodic-yearly",
        tier_label="yearly",
        pass_kind="best_of_n_judge",
        template="""You are picking the best of N candidate yearly summaries.

Each candidate synthesizes the same source material. Pick the one that:
- Best captures the year's major themes (NOT a recitation of every event)
- Maintains factual accuracy with the source monthlies
- Reads as coherent narrative, not a bulleted list
- Preserves Eva's voice and terminology
- Will be useful when read 2+ years from now

Source monthlies for verification:
{{source_text}}

Candidates:
{{candidates}}

VERDICT:
- Winner: <0-indexed integer>
- Reasoning: <2-3 sentences>
- Concerns about winner: <any factual issues to flag>""",
        notes="v4.1 §12 default — yearly best_of_n judge. Output format: 'Winner: N\\nReasoning: ...\\nConcerns: ...'",
    ),
    # LCM Wave-9 (2026-03-08): episodic-condensed/custom — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="episodic-condensed",
        tier_label="custom",
        pass_kind="single",
        template="""You are condensing a set of leaf summaries into a coherent narrative for an agent's memory pass.

The leaves below were selected by the agent based on a query or a time window. Produce a single synthesized summary that:
- Captures the major decisions and actions across these leaves
- Preserves concrete details (file paths, PRs, commits, commands) — do NOT abstract them away
- Identifies any recurring themes
- Notes any contradictions across leaves (if leaf A says X then leaf B says Y, surface both)
- Preserves Eva's actual phrasing where it captures nuance

Citations: include source leaf IDs in [bracket] notation after each major claim where helpful.

Length: target 1500-3000 tokens.

LEAF SUMMARIES:
{{source_text}}

SYNTHESIZED MEMORY PASS:""",
        notes="v4.1 §12 default — custom tier, used by lcm_synthesize_around for both time and semantic windows.",
    ),
    # LCM Wave-9 (2026-03-08): episodic-condensed/filtered — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="episodic-condensed",
        tier_label="filtered",
        pass_kind="single",
        template="""You are condensing a set of leaf summaries that were filtered by an agent grep query.

The leaves below all matched the grep filter. Produce a synthesized summary that:
- Captures what the matched leaves have in common (and any divergences)
- Preserves concrete details (file paths, PRs, commits, commands)
- Identifies any patterns specific to the filter context
- Notes contradictions across leaves where present

Citations: include source leaf IDs in [bracket] notation where helpful.

Length: target 1000-2500 tokens.

FILTERED LEAF SUMMARIES:
{{source_text}}

SYNTHESIZED FILTER PASS:""",
        notes="v4.1 §12 default — filtered tier, used when source set came from grep filter.",
    ),
    # LCM Wave-9 (2026-03-08): procedural-extract — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="procedural-extract",
        tier_label=None,
        pass_kind="single",
        template="""You are extracting a recurring procedure from a cluster of leaf summaries.

Input: leaves that an embedding-clustering algorithm grouped together. They MAY describe the same procedure performed at different times, OR they may not — your job is to determine which.

For the cluster:
- Determine: does this represent a single coherent procedure? (is_procedure: true/false)
- If yes:
  - Name the procedure (canonical form, lowercase, hyphen-separated, e.g. "gateway-rebuild")
  - List the steps in order (what gets done, in what sequence)
  - Confidence (0-1): how certain that this IS a recurring procedure vs noise

LEAVES:
{{source_text}}

OUTPUT (JSON):
{
  "is_procedure": <bool>,
  "name": <string|null>,
  "steps": [<string>, ...],
  "confidence": <0-1>
}""",
        notes="v4.1 §12 default — procedural extraction. Output strict JSON.",
    ),
    # LCM Wave-9 (2026-03-08): entity-extract — no {{date_range}} or {{target_length}}.
    SeedPromptDef(
        memory_type="entity-extract",
        tier_label=None,
        pass_kind="single",
        template="""You are extracting named entities from a leaf summary.

Entities to extract:
- People (Eva, Andrew, named collaborators)
- Projects (electric-sheep, lossless-claw, etc.)
- PRs (PR #1873, #74796, etc.)
- Commits (SHA fragments)
- Files (paths, AGENTS.md, etc.)
- Tools/services (openclaw-gateway, Voyage, etc.)
- Concepts (LCM, session_key, compaction, etc.)
- Config flags, error codes, agent IDs (R-XXX), bug numbers
- Anything else that is a NAMED THING (not a generic noun)

For each entity:
- text: the surface form as it appears
- type: one of the above categories OR a freeform new type
- span_start, span_end: character offsets in the leaf

LEAF:
{{source_text}}

OUTPUT (JSON array):
[{
  "text": <string>,
  "type": <string>,
  "span_start": <int>,
  "span_end": <int>
}, ...]""",
        notes="v4.1 §12 default — entity extraction. Output strict JSON array.",
    ),
)


# ---------------------------------------------------------------------------
# Public seed function
# ---------------------------------------------------------------------------


def seed_default_prompts(db: sqlite3.Connection) -> SeedResult:
    """Idempotently seed the §12 default prompts into :sql:`lcm_prompt_registry`.

    For each row in :data:`DEFAULT_PROMPTS`: if **any row** (active or
    archived) already exists for the ``(memory_type, tier_label,
    pass_kind)`` triple, skip; otherwise insert a v1 row with
    ``active = 1`` and ``bundle_version = 1``.

    **Composable with outer transactions.** The function uses raw
    ``INSERT`` statements directly (NOT :func:`register_prompt`) so it
    runs INSIDE the migration's ``BEGIN EXCLUSIVE`` without a nested-tx
    error. Two consecutive calls return ``SeedResult(seeded=N, skipped=0)``
    then ``SeedResult(seeded=0, skipped=N)``.

    Args:
        db: Open :class:`sqlite3.Connection`. May be inside an outer
            transaction — the function does NOT open one of its own.

    Returns:
        A :class:`SeedResult` summarizing the per-call counts.
    """

    seeded = 0
    skipped = 0

    for definition in DEFAULT_PROMPTS:
        # Check whether any row (active or archived) exists for the triple.
        # NULL tier_label requires `IS NULL` because `tier_label = NULL` is
        # always false in SQL.
        if definition.tier_label is None:
            existing = db.execute(
                "SELECT prompt_id FROM lcm_prompt_registry"
                " WHERE memory_type = ? AND tier_label IS NULL AND pass_kind = ?"
                " LIMIT 1",
                (definition.memory_type, definition.pass_kind),
            ).fetchone()
        else:
            existing = db.execute(
                "SELECT prompt_id FROM lcm_prompt_registry"
                " WHERE memory_type = ? AND tier_label = ? AND pass_kind = ?"
                " LIMIT 1",
                (definition.memory_type, definition.tier_label, definition.pass_kind),
            ).fetchone()

        if existing is not None:
            skipped += 1
            continue

        # No row — insert v1 directly (no nested BEGIN).
        prompt_id = f"pr_{secrets.token_hex(3)}"
        notes = definition.notes if definition.notes is not None else "v4.1 §12 default seed"
        db.execute(
            "INSERT INTO lcm_prompt_registry"
            " (prompt_id, memory_type, tier_label, pass_kind, version, template,"
            "  model_recommendation, active, bundle_version, notes)"
            " VALUES (?, ?, ?, ?, 1, ?, ?, 1, 1, ?)",
            (
                prompt_id,
                definition.memory_type,
                definition.tier_label,
                definition.pass_kind,
                definition.template,
                definition.model_recommendation,
                notes,
            ),
        )
        seeded += 1

    return SeedResult(seeded=seeded, skipped=skipped)
