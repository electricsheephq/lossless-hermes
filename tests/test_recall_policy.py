"""Tests for :mod:`lossless_hermes.recall_policy` (issue 03-10).

Covers the recall-policy text constant + the user-voice rewording
function. The TS source (``lossless-claw/src/plugin/index.ts:244-321``,
commit ``1f07fbd``) is the canonical reference — the
:data:`_RAW_POLICY_PROMPT` constant ports it verbatim and the
:data:`LOSSLESS_RECALL_POLICY_PROMPT` constant is the user-voice form
that the ``pre_llm_call`` hook returns per ADR-014.

These tests assert:

* The raw TS-verbatim form preserves the upstream text byte-for-byte
  (modulo the documented user-voice substitutions). A hash-based
  snapshot pins the raw text so future edits surface as a deliberate
  test-update, not silent drift.
* The user-voice form differs from the raw form only via the
  documented :data:`_USER_VOICE_REPLACEMENTS` table.
* Every behavioral instruction in the raw text survives the rewording
  (the tool names, the escalation order, the precision flow).
* The rewording function is idempotent — applying it twice yields the
  same output as applying it once. This guards against future
  replacements whose ``replace`` clause re-matches the ``find``.

See:

* ``docs/adr/014-recall-policy-injection.md`` — voice-shift rationale.
* ``epics/03-ingest-assembly/03-10-recall-policy-injection.md`` — AC.
"""

from __future__ import annotations

import hashlib
import re

from lossless_hermes.recall_policy import (
    LOSSLESS_RECALL_POLICY_PROMPT,
    reword_for_user_voice,
)
from lossless_hermes.recall_policy import (
    _RAW_POLICY_LINES,  # type: ignore[attr-defined]
    _RAW_POLICY_PROMPT,  # type: ignore[attr-defined]
    _USER_VOICE_REPLACEMENTS,  # type: ignore[attr-defined]
)
from lossless_hermes.tools import get_tool_schemas


# ---------------------------------------------------------------------------
# Raw text matches TS verbatim
# ---------------------------------------------------------------------------


def test_raw_policy_first_line_is_title() -> None:
    """The TS source opens with ``## Lossless Recall Policy`` —
    ``lossless-claw/src/plugin/index.ts:245``. The raw constant must
    preserve that opener byte-for-byte.
    """
    assert _RAW_POLICY_LINES[0] == "## Lossless Recall Policy"


def test_raw_policy_second_line_is_blank() -> None:
    """Line 246 of the TS source is an empty string between the title
    and the activation phrase. Pinning this catches future edits that
    collapse the blank-line separators."""
    assert _RAW_POLICY_LINES[1] == ""


def test_raw_policy_activation_phrase_matches_ts() -> None:
    """Line 247: ``"The lossless-claw plugin is active."`` — the
    activation phrase the user-voice reword transforms."""
    assert _RAW_POLICY_LINES[2] == "The lossless-claw plugin is active."


def test_raw_policy_supersession_clause_matches_ts() -> None:
    """Line 249 carries the precedence rule for compacted history. Pin
    the wording so future TS pulls surface here on diff."""
    assert (
        "For compacted conversation history, these instructions supersede" in _RAW_POLICY_LINES[4]
    )
    assert "Prefer lossless-claw recall tools first" in _RAW_POLICY_LINES[4]


def test_raw_policy_escalation_order_is_grep_describe_deep_recall() -> None:
    """The 1/2/3 recall escalation ladder is grep → describe → deep-recall.

    The TS source's step 3 named ``lcm_expand_query``; per the v0.1.1
    ADR-012 fix that tool is deferred + unregistered, so step 3 now
    routes deep recall through ``lcm_describe``'s one-hop expand flags.
    The exact numbering + ordering remain part of the policy contract;
    pin them so a reword cannot reorder them silently.
    """
    raw = _RAW_POLICY_PROMPT
    grep_idx = raw.find("1. `lcm_grep`")
    describe_idx = raw.find("2. `lcm_describe`")
    # v0.1.1: step 3 is the lcm_describe expand-flags deep-recall line.
    deep_recall_idx = raw.find("3. `lcm_describe` with `expandChildren=true`")
    assert grep_idx >= 0, "raw policy missing lcm_grep escalation step 1"
    assert describe_idx > grep_idx, "raw policy out-of-order (describe before grep)"
    assert deep_recall_idx > describe_idx, "raw policy out-of-order (step 3 before step 2)"


def test_raw_policy_line_count() -> None:
    """Pin the line count of the (v0.1.1-edited) raw policy text.

    The TS source had 76 array entries on lines 245-320 of
    ``src/plugin/index.ts``. The v0.1.1 ADR-012 fix removed the
    7-line dedicated ``lcm_expand_query`` usage block (the other
    ``lcm_expand_query`` references were reworded in place, 1:1), so
    the Python port now has 69 lines. Pin it so a future edit adding
    or dropping lines surfaces here.
    """
    # 76 TS entries minus the 7-line lcm_expand_query usage block = 69.
    assert len(_RAW_POLICY_LINES) == 69


def test_raw_policy_prompt_is_join_with_newline() -> None:
    """The TS source joins the array with ``"\\n"`` (line 321:
    ``.join("\\n")``). The Python port must produce the same byte
    sequence so that downstream consumers see identical text."""
    assert _RAW_POLICY_PROMPT == "\n".join(_RAW_POLICY_LINES)


def test_raw_policy_hash_snapshot() -> None:
    """SHA-256 snapshot of the raw policy text. Catches silent drift.

    Update the expected hash deliberately when a TS change ports
    forward OR when a deliberate, provenance-commented edit lands.

    Original hash (TS pin ``lossless-claw/src/plugin/index.ts:244-321``,
    commit ``1f07fbd``, branch ``pr-613``):
    ``708330c9fde10395fe7e91b9b03fb05d9fbf55e70e72eccc131e0920cab3ea4b``.

    v0.1.1 — the hash changed because the ADR-012 fix rewrote every
    ``lcm_expand_query`` reference (deferred + unregistered tool) out of
    the per-turn policy text. See the ``# ADR-012`` provenance comments
    in :data:`lossless_hermes.recall_policy._RAW_POLICY_LINES`.
    """
    expected = "12a7199c0e218046bc83295e1f0a4563a740556e94ad7ef2f127b2209137987c"
    actual = hashlib.sha256(_RAW_POLICY_PROMPT.encode("utf-8")).hexdigest()
    # First assert the hash matches; if it doesn't, the failure message
    # gives the actual hash for an intentional update.
    assert actual == expected, (
        f"raw policy text drifted from the v0.1.1 snapshot. "
        f"actual={actual}, expected={expected}. "
        f"If this drift is intentional (TS pulled forward, or a "
        f"deliberate provenance-commented edit), update the expected "
        f"hash here."
    )


# ---------------------------------------------------------------------------
# User-voice rewording — the documented transformation
# ---------------------------------------------------------------------------


def test_user_voice_replacements_table_is_non_empty() -> None:
    """The replacements table is the audit-friendly diff between
    system-voice and user-voice. An empty table would mean the user-
    voice form is byte-identical to the system-voice form, defeating
    the whole point of the ADR-014 rewording. Pin presence."""
    assert len(_USER_VOICE_REPLACEMENTS) >= 1


def test_user_voice_replacement_project_name() -> None:
    """The first replacement is the project-name swap from the TS
    package (``lossless-claw``) to the Python distribution
    (``lossless-hermes``). Pin its presence so a future edit cannot
    accidentally remove it and ship policy text referencing the wrong
    package."""
    finds = {find for find, _ in _USER_VOICE_REPLACEMENTS}
    assert "lossless-claw" in finds, "project-name swap missing from reword table"


def test_user_voice_form_contains_lossless_hermes_not_claw() -> None:
    """The shipped policy text references the Python distribution name,
    not the TS package name. This is a downstream invariant from the
    project-name swap — pin it directly so the test fails if the swap
    breaks or a future raw-text addition forgets to use the new name.
    """
    assert "lossless-claw" not in LOSSLESS_RECALL_POLICY_PROMPT
    assert "Lossless-claw" not in LOSSLESS_RECALL_POLICY_PROMPT
    assert "lossless-hermes" in LOSSLESS_RECALL_POLICY_PROMPT


def test_user_voice_form_title_softened_to_guidance() -> None:
    """The TS title ``## Lossless Recall Policy`` reads as a system-
    issued rule-set; the user-voice form softens to ``## Lossless
    Recall Guidance`` per ADR-014. Pin both removal and replacement."""
    assert "## Lossless Recall Policy" not in LOSSLESS_RECALL_POLICY_PROMPT
    assert "## Lossless Recall Guidance" in LOSSLESS_RECALL_POLICY_PROMPT


def test_user_voice_form_activation_phrase_is_user_voice() -> None:
    """The TS activation phrase ``"The lossless-claw plugin is active."``
    reads as a system status line. The user-voice rewording frames the
    rest as available capability rather than an active directive.
    """
    assert "The lossless-claw plugin is active." not in LOSSLESS_RECALL_POLICY_PROMPT
    assert "The lossless-hermes plugin is active." not in LOSSLESS_RECALL_POLICY_PROMPT
    # The user-voice opener phrases it as user-supplied framing.
    assert (
        "The lossless-hermes recall tools are available for this conversation"
        in LOSSLESS_RECALL_POLICY_PROMPT
    )


def test_user_voice_form_preserves_tool_names() -> None:
    """Every tool name in the policy must survive the reword.

    These are the load-bearing identifiers the model uses to call the
    actual tools — drift here would silently break tool calls.

    v0.1.1: ``lcm_expand_query`` is intentionally NOT in this set — the
    ADR-012 fix removed it from the policy text because it is deferred
    and unregistered (see :func:`test_user_voice_form_has_no_unregistered_tool`).
    """
    for tool in (
        "lcm_grep",
        "lcm_describe",
        "lcm_synthesize_around",
        "lcm_get_entity",
        "lcm_search_entities",
    ):
        assert tool in LOSSLESS_RECALL_POLICY_PROMPT, (
            f"tool name {tool!r} did not survive user-voice rewording"
        )


def test_user_voice_form_preserves_escalation_order() -> None:
    """The 1/2/3 recall escalation order must survive the reword.

    Same invariant as
    ``test_raw_policy_escalation_order_is_grep_describe_deep_recall``
    but asserted on the public user-voice constant. v0.1.1: step 3 is
    the ``lcm_describe`` expand-flags deep-recall line (the deferred
    ``lcm_expand_query`` was removed per ADR-012)."""
    text = LOSSLESS_RECALL_POLICY_PROMPT
    grep_idx = text.find("1. `lcm_grep`")
    describe_idx = text.find("2. `lcm_describe`")
    deep_recall_idx = text.find("3. `lcm_describe` with `expandChildren=true`")
    assert grep_idx >= 0
    assert describe_idx > grep_idx
    assert deep_recall_idx > describe_idx


def test_user_voice_form_preserves_precision_flow() -> None:
    """The 1/2/3 precision-flow block from TS lines 307-309 must
    survive into the user-voice text. This is the load-bearing
    "expand before asserting specifics" advice."""
    assert "**Precision flow:**" in LOSSLESS_RECALL_POLICY_PROMPT
    assert "lcm_grep` to find the relevant" in LOSSLESS_RECALL_POLICY_PROMPT


def test_user_voice_form_preserves_uncertainty_checklist() -> None:
    """The uncertainty checklist from TS lines 312-316 must survive
    into the user-voice text. The 3 bullets are how the model decides
    whether to expand before answering."""
    text = LOSSLESS_RECALL_POLICY_PROMPT
    assert "**Uncertainty checklist:**" in text
    assert "Am I making an exact factual claim from compacted context?" in text
    assert "Could compaction have omitted a crucial detail?" in text


def test_user_voice_form_length_close_to_raw() -> None:
    """Sanity-check that the user-voice form is roughly the same size
    as the raw form. A >20% size jump means the rewording is doing
    much more than the documented small substitutions and the table
    should be re-reviewed."""
    raw_len = len(_RAW_POLICY_PROMPT)
    voice_len = len(LOSSLESS_RECALL_POLICY_PROMPT)
    ratio = abs(voice_len - raw_len) / raw_len
    assert ratio < 0.20, (
        f"user-voice form length differs from raw by {ratio:.1%} "
        f"(raw={raw_len}, voice={voice_len}). Reword table may have "
        f"grown beyond documented scope — review."
    )


# ---------------------------------------------------------------------------
# reword_for_user_voice() — function-level contracts
# ---------------------------------------------------------------------------


def test_reword_for_user_voice_returns_raw_for_empty_input() -> None:
    """Function-level contract: empty input stays empty (no replacement
    can fire on an empty string)."""
    assert reword_for_user_voice("") == ""


def test_reword_for_user_voice_is_idempotent() -> None:
    """Applying the reword twice equals applying it once. Guards
    against future replacements where the ``replace`` clause still
    contains the ``find`` clause (which would double-substitute).
    """
    once = reword_for_user_voice(_RAW_POLICY_PROMPT)
    twice = reword_for_user_voice(once)
    assert once == twice, (
        "reword_for_user_voice is not idempotent — a replacement's "
        "right-hand side still matches its left-hand side"
    )


def test_reword_for_user_voice_applies_project_name_swap() -> None:
    """Direct unit test for the project-name replacement. Passing in
    a minimal string lets us verify the swap fires without depending
    on the full policy text being involved."""
    out = reword_for_user_voice("hello from the lossless-claw plugin")
    assert "lossless-claw" not in out
    assert "lossless-hermes" in out


def test_reword_for_user_voice_leaves_unrelated_text_unchanged() -> None:
    """Text that doesn't contain any of the ``find`` clauses must pass
    through unchanged. Guards against an over-eager replacement that
    munges unrelated content."""
    irrelevant = "This sentence is unrelated to the recall policy."
    assert reword_for_user_voice(irrelevant) == irrelevant


def test_lossless_recall_policy_prompt_is_reworded_raw() -> None:
    """The public constant is exactly the reword applied to the raw
    constant. Pin the relationship so a future refactor cannot
    accidentally bypass the function."""
    assert LOSSLESS_RECALL_POLICY_PROMPT == reword_for_user_voice(_RAW_POLICY_PROMPT)


# ---------------------------------------------------------------------------
# v0.1.1 P1 — the policy must not advertise an unregistered tool
# ---------------------------------------------------------------------------
#
# The recall-policy prompt is injected into the model's context EVERY turn
# via the ``pre_llm_call`` hook (``engine/assemble.py:_on_pre_llm_call``).
# Per ADR-012, ``lcm_expand_query`` is deferred to v0.2.0 and is NOT in
# ``TOOL_SCHEMAS`` — so any mention of it in this text told the model, every
# turn, to call a tool it could not see in its tool list. The v0.1.1 fix
# rewrote those references out. These tests pin that fix and, more
# generally, assert the policy never names a tool the engine doesn't expose.


def _tool_names_in_prose(text: str) -> set[str]:
    """Extract every ``lcm_*`` tool identifier mentioned in ``text``.

    The recall-policy prose names tools in backtick spans (e.g.
    ```lcm_grep```) and occasionally bare. This scans for the
    ``lcm_<identifier>`` token shape regardless of surrounding
    backticks/parentheses so the membership check below cannot be
    fooled by a punctuation variation.

    Returns:
        The set of distinct ``lcm_*`` identifiers found.
    """
    # ``lcm_`` followed by one or more identifier chars. Tool names are
    # snake_case ASCII; this matches lcm_grep, lcm_describe, lcm_expand,
    # lcm_expand_query, lcm_synthesize_around, lcm_get_entity, etc.
    return set(re.findall(r"\blcm_[a-z_]+", text))


def test_user_voice_form_has_no_unregistered_tool() -> None:
    """The shipped recall-policy prompt never names ``lcm_expand_query``.

    v0.1.1 P1 regression: ``lcm_expand_query`` is deferred + unregistered
    per ADR-012. Because the policy text is injected every turn, naming
    that tool instructed the model to call something absent from its
    tool list. Assert the rendered (user-voice) prompt is clean.
    """
    assert "lcm_expand_query" not in LOSSLESS_RECALL_POLICY_PROMPT, (
        "recall-policy prompt still advertises lcm_expand_query — that "
        "tool is deferred + unregistered per ADR-012, and this text is "
        "injected into the model's context every turn."
    )


def test_raw_policy_form_has_no_unregistered_tool() -> None:
    """The raw (pre-reword) policy text also never names ``lcm_expand_query``.

    The user-voice reword does not touch ``lcm_expand_query`` (it is not
    in :data:`_USER_VOICE_REPLACEMENTS`), so a clean user-voice form
    implies a clean raw form — but pin the raw constant directly so a
    future raw-text edit re-introducing the reference is caught at the
    source, not only downstream.
    """
    assert "lcm_expand_query" not in _RAW_POLICY_PROMPT


def test_every_tool_named_in_policy_is_registered() -> None:
    """Every ``lcm_*`` tool the policy prose names is in ``TOOL_SCHEMAS``.

    The stronger invariant behind the P1 fix: the per-turn recall-policy
    prompt must never advertise *any* tool the engine does not actually
    expose to the model — not just ``lcm_expand_query``. This walks
    every ``lcm_*`` identifier in the rendered prose and asserts it is a
    registered tool name. If a future edit names a new/renamed/deferred
    tool, this fails loudly.
    """
    registered = {s["name"] for s in get_tool_schemas()}
    named = _tool_names_in_prose(LOSSLESS_RECALL_POLICY_PROMPT)

    # Sanity floor: the policy must name at least the core recall tools,
    # otherwise an empty-prose regression would vacuously pass.
    assert {"lcm_grep", "lcm_describe"} <= named, (
        f"recall-policy prose names too few tools ({sorted(named)}) — "
        f"expected at least lcm_grep + lcm_describe. Prose may have "
        f"regressed."
    )

    unregistered = named - registered
    assert not unregistered, (
        f"recall-policy prompt advertises unregistered tool(s): "
        f"{sorted(unregistered)}. The prompt is injected every turn; it "
        f"must only name tools present in TOOL_SCHEMAS "
        f"({sorted(registered)}). Per ADR-012, lcm_expand_query is "
        f"deferred — do not reference it (or any other unregistered "
        f"tool) in model-facing prose."
    )
