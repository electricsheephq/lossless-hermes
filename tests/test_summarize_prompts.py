"""Tests for :mod:`lossless_hermes.summarize` (issue 04-05).

Covers the three load-bearing prompt builders the LCM summarizer uses:

* :func:`build_leaf_prompt` — leaf-tier (segment) prompt.
* :func:`build_condensed_prompt` — condensed-tier (D1 / D2 / D3+).
* :func:`build_deterministic_fallback` — non-LLM fallback (Wave-4 / Wave-9
  marker invariants).

### Why SHA-256 snapshot tests

Per ``docs/porting-guides/assembler-compaction.md`` §"Prompt templates",
these strings are **load-bearing operator tuning**. A subtle whitespace
or wording drift in the port would produce summaries that pass type
checks and behavioral assertions but degrade quality in production. The
SHA-256 snapshot tests pin each canonical prompt to its TS-verified hash
so any byte-level drift surfaces as a deliberate test-update, not a
quiet quality regression.

The expected hashes were computed from the TS source pinned to commit
``1f07fbd`` (branch ``pr-613``) and cross-verified by running an
``esbuild``-transpiled copy of the TS builders alongside the Python port
with identical inputs. The reference hashes therefore prove byte-for-byte
parity between the TS and Python prompt outputs.

### Test taxonomy

* **Snapshot tests** (`test_*_snapshot`) — SHA-256 lock on canonical
  inputs. The "if this fails, update the hash" message points at the
  TS source so the reviewer can confirm the drift is intentional.
* **Dispatch tests** (`test_*_dispatch_*`) — verify the
  ``build_condensed_prompt`` depth-routing picks D1 / D2 / D3+ at the
  documented depth boundaries.
* **Behavioral tests** (`test_*`) — check Wave-N invariants (marker
  always present, 256 floor, distinct markers) and the small set of
  contract-level surface guarantees (XML tag presence, target-tokens
  interpolation, custom-instruction substitution).

See:

* ``epics/04-compaction/04-05-summarize-prompt-templates.md`` — AC.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment policy.
* ``lossless-claw/src/summarize.ts:881-1102`` — TS source.
"""

from __future__ import annotations

import hashlib

from lossless_hermes.summarize import (
    LCM_SUMMARIZER_SYSTEM_PROMPT,
    build_condensed_prompt,
    build_deterministic_fallback,
    build_leaf_prompt,
)
from lossless_hermes.summarize import (
    _FALLBACK_MARKER,  # type: ignore[attr-defined]
    _FALLBACK_MARKER_TRUNC,  # type: ignore[attr-defined]
)


def _sha256(s: str) -> str:
    """Convenience wrapper — UTF-8 encode + hex digest."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# LCM_SUMMARIZER_SYSTEM_PROMPT — system-prompt constant
# ---------------------------------------------------------------------------


def test_system_prompt_snapshot() -> None:
    """SHA-256 snapshot of :data:`LCM_SUMMARIZER_SYSTEM_PROMPT`.

    TS source: ``lossless-claw/src/summarize.ts:59-60`` (commit
    ``1f07fbd``). Cross-verified against the TS string via ``esbuild``
    bundle of the literal.
    """
    expected = "70be2991700ea7830486ac6e24ed50ff206cad0cdd96daafe648f9b99246d9b6"
    actual = _sha256(LCM_SUMMARIZER_SYSTEM_PROMPT)
    assert actual == expected, (
        f"LCM_SUMMARIZER_SYSTEM_PROMPT drifted from TS pin (commit 1f07fbd). "
        f"actual={actual}, expected={expected}. "
        f"If intentional, recompute via the TS source and update."
    )


def test_system_prompt_contains_engine_phrase() -> None:
    """The TS test in ``lossless-claw/test/summarize.test.ts:408`` pins
    the phrase ``"context-compaction summarization engine"`` as the
    signature this is the LCM system prompt and not some other generic
    one. Mirror the assertion."""
    assert "context-compaction summarization engine" in LCM_SUMMARIZER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# build_leaf_prompt — leaf (segment) prompt
# ---------------------------------------------------------------------------


def test_leaf_prompt_normal_snapshot() -> None:
    """SHA-256 snapshot of the canonical normal-mode leaf prompt.

    Canonical fixture:
        text="Hello world", mode="normal", target_tokens=600,
        previous_summary=None, custom_instructions=None.

    Hash computed from the byte-identical TS implementation via an
    ``esbuild``-transpiled run of ``buildLeafSummaryPrompt`` from
    ``lossless-claw/src/summarize.ts:881-928`` (commit ``1f07fbd``).
    """
    expected = "1a6d7446b50d98f2db4b5736e7f5354b8488e16ffb46059d91fcda9185d776a6"
    prompt = build_leaf_prompt(
        text="Hello world",
        mode="normal",
        target_tokens=600,
    )
    actual = _sha256(prompt)
    assert actual == expected, (
        f"build_leaf_prompt(normal) drifted from TS pin (commit 1f07fbd). "
        f"actual={actual}, expected={expected}."
    )


def test_leaf_prompt_aggressive_snapshot() -> None:
    """SHA-256 snapshot of the canonical aggressive-mode leaf prompt.

    Same canonical inputs as :func:`test_leaf_prompt_normal_snapshot`
    but with ``mode="aggressive"`` — this snapshot also implicitly
    asserts that the policy block is the only difference between modes.
    """
    expected = "8b045b4201d06d24a4df1f8eb19b02528f5b43de63c41aa626c8af2db4e85fe6"
    prompt = build_leaf_prompt(
        text="Hello world",
        mode="aggressive",
        target_tokens=600,
    )
    actual = _sha256(prompt)
    assert actual == expected, (
        f"build_leaf_prompt(aggressive) drifted from TS pin (commit 1f07fbd). "
        f"actual={actual}, expected={expected}."
    )


def test_leaf_prompt_normal_vs_aggressive_differ() -> None:
    """The TS test at ``summarize.test.ts:381`` asserts the normal and
    aggressive prompts are visibly distinct (different policy blocks).
    Mirror that assertion."""
    normal = build_leaf_prompt(text="x", mode="normal", target_tokens=400)
    aggressive = build_leaf_prompt(text="x", mode="aggressive", target_tokens=400)
    assert normal != aggressive
    assert "Normal summary policy:" in normal
    assert "Aggressive summary policy:" in aggressive
    assert "Aggressive summary policy:" not in normal
    assert "Normal summary policy:" not in aggressive


def test_leaf_prompt_includes_openclaw_header_per_adr_024() -> None:
    """Per ADR-024 the literal "OpenClaw" string must survive the port
    — agents see "OpenClaw" in their compaction summaries and a
    port-time wording divergence to "Hermes" would shift the agent's
    framing in untested ways.

    Pin the exact header phrase from
    ``lossless-claw/src/summarize.ts:911``.
    """
    prompt = build_leaf_prompt(text="x", mode="normal", target_tokens=400)
    assert "You summarize a SEGMENT of an OpenClaw conversation for future model turns." in prompt


def test_leaf_prompt_target_tokens_is_interpolated() -> None:
    """The TS template literal interpolates ``targetTokens`` into the
    last bullet of the Output-requirements block: ``"- Target length:
    about {N} tokens or less."`` (summarize.ts:923). Pin the exact
    wording — downstream length-cue parsing depends on it.
    """
    prompt = build_leaf_prompt(text="x", mode="normal", target_tokens=2400)
    assert "- Target length: about 2400 tokens or less." in prompt


def test_leaf_prompt_files_none_literal_is_present() -> None:
    """Per the porting guide §"Prompt templates", the literal phrase
    ``"Files: none"`` is part of the operator-tuning contract — drift
    here would break the file-operations-empty case the operators
    audited against. Pin the exact wording.
    """
    prompt = build_leaf_prompt(text="x", mode="normal", target_tokens=400)
    assert '- If no file operations appear, include exactly: "Files: none".' in prompt


def test_leaf_prompt_expand_for_details_end_marker_present() -> None:
    """Per the porting guide, the ``"Expand for details about:"`` end
    marker is load-bearing — the recall policy and downstream readers
    use it as a cue to expand before asserting specifics."""
    prompt = build_leaf_prompt(text="x", mode="normal", target_tokens=400)
    assert (
        '- End with exactly: "Expand for details about: '
        '<comma-separated list of what was dropped or compressed>".\n'
    ) in prompt + "\n"
    assert "Expand for details about:" in prompt


def test_leaf_prompt_previous_context_xml_wrapper_present() -> None:
    """The ``<previous_context>``...``</previous_context>`` XML tag is
    part of the prompt contract — parsers and downstream tooling key on
    these wrapper tags. Pin presence."""
    prompt = build_leaf_prompt(
        text="x",
        mode="normal",
        target_tokens=400,
        previous_summary="An earlier summary.",
    )
    assert "<previous_context>\nAn earlier summary.\n</previous_context>" in prompt


def test_leaf_prompt_conversation_segment_xml_wrapper_present() -> None:
    """The ``<conversation_segment>`` XML tag wraps the source text the
    model is meant to summarize. Pin presence."""
    prompt = build_leaf_prompt(text="THE_SOURCE", mode="normal", target_tokens=400)
    assert "<conversation_segment>\nTHE_SOURCE\n</conversation_segment>" in prompt


def test_leaf_prompt_previous_summary_none_renders_as_none_sentinel() -> None:
    """The TS form is ``previousSummary?.trim() || "(none)"`` — both
    ``None`` and a whitespace-only string fall through to the
    ``"(none)"`` sentinel. Pin the sentinel."""
    prompt = build_leaf_prompt(text="x", mode="normal", target_tokens=400)
    assert "<previous_context>\n(none)\n</previous_context>" in prompt


def test_leaf_prompt_whitespace_only_previous_summary_renders_as_none() -> None:
    """Whitespace-only ``previous_summary`` mirrors the JS truthy
    coercion: empty after ``.trim()`` → ``"(none)"`` sentinel.
    """
    prompt = build_leaf_prompt(
        text="x",
        mode="normal",
        target_tokens=400,
        previous_summary="   \n\t  ",
    )
    assert "<previous_context>\n(none)\n</previous_context>" in prompt


def test_leaf_prompt_previous_summary_is_trimmed_before_substitution() -> None:
    """``previousSummary.trim()`` runs before interpolation — leading
    or trailing whitespace from the caller is dropped inside the XML
    wrapper so the model sees the cleaned form. Pin trimming."""
    prompt = build_leaf_prompt(
        text="x",
        mode="normal",
        target_tokens=400,
        previous_summary="  trimmed text  ",
    )
    assert "<previous_context>\ntrimmed text\n</previous_context>" in prompt


def test_leaf_prompt_custom_instructions_substituted() -> None:
    """The custom-instructions branch builds a block of the form
    ``"Operator instructions:\\n{trimmed_text}"``. Pin shape +
    substitution."""
    prompt = build_leaf_prompt(
        text="x",
        mode="normal",
        target_tokens=400,
        custom_instructions="Keep implementation caveats.",
    )
    assert "Operator instructions:\nKeep implementation caveats." in prompt


def test_leaf_prompt_custom_instructions_none_renders_none_block() -> None:
    """No custom instructions → ``"Operator instructions: (none)"``
    literal. Pin the wording (the colon-space vs the multiline variant
    distinguishes the two branches)."""
    prompt = build_leaf_prompt(text="x", mode="normal", target_tokens=400)
    assert "Operator instructions: (none)" in prompt


def test_leaf_prompt_whitespace_only_custom_instructions_renders_none() -> None:
    """Whitespace-only custom_instructions follows the JS truthy
    coercion path: ``""`` after ``.trim()`` → ``"Operator instructions:
    (none)"``."""
    prompt = build_leaf_prompt(
        text="x",
        mode="normal",
        target_tokens=400,
        custom_instructions="   ",
    )
    assert "Operator instructions: (none)" in prompt


# ---------------------------------------------------------------------------
# build_condensed_prompt — depth-routed condensed prompts
# ---------------------------------------------------------------------------


def test_condensed_prompt_d1_snapshot() -> None:
    """SHA-256 snapshot of the canonical D1 (depth ≤ 1) prompt with no
    ``previous_summary``.

    TS source: ``lossless-claw/src/summarize.ts:930-978``
    (``buildD1Prompt``, commit ``1f07fbd``).
    """
    expected = "ddda911c9dd0c529dc38ebefa289f7e0e306d2c794bb2dc8999885937c71e82a"
    prompt = build_condensed_prompt(
        text="Hello world",
        target_tokens=400,
        depth=1,
    )
    actual = _sha256(prompt)
    assert actual == expected, (
        f"build_condensed_prompt(depth=1) drifted from TS pin (commit 1f07fbd). "
        f"actual={actual}, expected={expected}."
    )


def test_condensed_prompt_d1_with_previous_context_snapshot() -> None:
    """SHA-256 snapshot of the canonical D1 prompt with a non-empty
    ``previous_summary``.

    The D1 path is the only one of the three condensed templates that
    consumes ``previous_summary`` — pinning the with-previous-context
    branch separately catches a regression where the previous-context
    block accidentally drops out of the assembly.
    """
    expected = "e66ca5742b736a4e7c99b3e4b7a0147f739a2ebb2e6aabcd5afcd819f14ff623"
    prompt = build_condensed_prompt(
        text="Hello world",
        target_tokens=400,
        depth=1,
        previous_summary="Earlier context.",
    )
    actual = _sha256(prompt)
    assert actual == expected, (
        f"build_condensed_prompt(depth=1, with previous_summary) drifted "
        f"from TS pin (commit 1f07fbd). actual={actual}, expected={expected}."
    )


def test_condensed_prompt_d2_snapshot() -> None:
    """SHA-256 snapshot of the canonical D2 (depth == 2) prompt.

    TS source: ``lossless-claw/src/summarize.ts:980-1014``
    (``buildD2Prompt``, commit ``1f07fbd``).
    """
    expected = "e3bf35746ad649948ed474b97fd6f597215ae00281052239aee9926ead6b8924"
    prompt = build_condensed_prompt(
        text="Hello world",
        target_tokens=400,
        depth=2,
    )
    actual = _sha256(prompt)
    assert actual == expected, (
        f"build_condensed_prompt(depth=2) drifted from TS pin (commit 1f07fbd). "
        f"actual={actual}, expected={expected}."
    )


def test_condensed_prompt_d3_plus_snapshot() -> None:
    """SHA-256 snapshot of the canonical D3+ (depth ≥ 3) prompt.

    TS source: ``lossless-claw/src/summarize.ts:1016-1050``
    (``buildD3PlusPrompt``, commit ``1f07fbd``).

    The depth value is NOT interpolated — depth 3, 4, 5+ all produce
    the same template (no per-depth numerical differences). The
    snapshot uses ``depth=3``.
    """
    expected = "b056d35af9773f8797d645515346647e4fa561b8296337ba5ee1569fc0ebd2f6"
    prompt = build_condensed_prompt(
        text="Hello world",
        target_tokens=400,
        depth=3,
    )
    actual = _sha256(prompt)
    assert actual == expected, (
        f"build_condensed_prompt(depth=3) drifted from TS pin (commit 1f07fbd). "
        f"actual={actual}, expected={expected}."
    )


def test_condensed_prompt_dispatch_depth_zero_is_d1() -> None:
    """``depth <= 1`` routes to D1; ``depth=0`` is the lower end of
    that branch. The TS guard at ``summarize.ts:1060`` uses ``<= 1``
    not ``< 1`` so depth 0 and 1 share the same template.
    """
    d0 = build_condensed_prompt(text="x", target_tokens=400, depth=0)
    d1 = build_condensed_prompt(text="x", target_tokens=400, depth=1)
    assert d0 == d1


def test_condensed_prompt_dispatch_depth_one_is_d1() -> None:
    """``depth == 1`` shares the D1 template. The header phrase pins
    which template was selected."""
    prompt = build_condensed_prompt(text="x", target_tokens=400, depth=1)
    assert (
        "You are compacting leaf-level conversation summaries into a single condensed memory node."
        in prompt
    )


def test_condensed_prompt_dispatch_depth_two_is_d2() -> None:
    """``depth == 2`` routes to D2 (distinct from D1 and D3+). The
    header phrase pins the template."""
    prompt = build_condensed_prompt(text="x", target_tokens=400, depth=2)
    assert (
        "You are condensing multiple session-level summaries into a higher-level memory node."
        in prompt
    )


def test_condensed_prompt_dispatch_depth_three_is_d3_plus() -> None:
    """``depth == 3`` routes to D3+. Header phrase pins the template."""
    prompt = build_condensed_prompt(text="x", target_tokens=400, depth=3)
    assert (
        "You are creating a high-level memory node from multiple phase-level summaries." in prompt
    )


def test_condensed_prompt_dispatch_depth_four_is_d3_plus_same_as_three() -> None:
    """``depth >= 3`` shares one template — 3, 4, 5+ all produce
    byte-identical output for the same ``text`` and ``target_tokens``.
    """
    d3 = build_condensed_prompt(text="x", target_tokens=400, depth=3)
    d4 = build_condensed_prompt(text="x", target_tokens=400, depth=4)
    d99 = build_condensed_prompt(text="x", target_tokens=400, depth=99)
    assert d3 == d4 == d99


def test_condensed_prompt_d1_includes_previous_context_when_provided() -> None:
    """D1 is the only condensed template that emits a
    ``<previous_context>`` block — and only when ``previous_summary``
    is non-empty. Pin both branches.
    """
    with_prev = build_condensed_prompt(
        text="x",
        target_tokens=400,
        depth=1,
        previous_summary="Earlier summary.",
    )
    without_prev = build_condensed_prompt(text="x", target_tokens=400, depth=1)
    assert "<previous_context>\nEarlier summary.\n</previous_context>" in with_prev
    assert "<previous_context>" not in without_prev
    # The without-prev branch carries the alternate framing line.
    assert "Focus on what matters for continuation:" in without_prev


def test_condensed_prompt_d2_omits_previous_context_even_if_provided() -> None:
    """D2 deliberately does NOT consume ``previous_summary`` — the TS
    dispatch at ``summarize.ts:1063`` forwards only ``text``,
    ``targetTokens``, and ``customInstructions``. Pin that ``D2``
    output is byte-identical regardless of ``previous_summary``.
    """
    a = build_condensed_prompt(text="x", target_tokens=400, depth=2)
    b = build_condensed_prompt(
        text="x",
        target_tokens=400,
        depth=2,
        previous_summary="This must not appear in output.",
    )
    assert a == b
    assert "<previous_context>" not in a
    assert "This must not appear in output." not in a


def test_condensed_prompt_d3_plus_omits_previous_context_even_if_provided() -> None:
    """Same invariant as D2 for D3+ — ``previous_summary`` is dropped
    on the way through ``build_condensed_prompt`` for depths ≥ 3."""
    a = build_condensed_prompt(text="x", target_tokens=400, depth=3)
    b = build_condensed_prompt(
        text="x",
        target_tokens=400,
        depth=3,
        previous_summary="This must not appear in output.",
    )
    assert a == b
    assert "<previous_context>" not in a


def test_condensed_prompt_target_tokens_interpolated_in_d1() -> None:
    """The condensed templates use ``"Target length: about N tokens."``
    (note: NO ``"or less"`` suffix — that's the leaf variant). Pin the
    exact wording at the D1 template.
    """
    prompt = build_condensed_prompt(text="x", target_tokens=1234, depth=1)
    assert "Target length: about 1234 tokens." in prompt
    # Make sure the leaf wording does not leak across.
    assert "Target length: about 1234 tokens or less." not in prompt


def test_condensed_prompt_target_tokens_interpolated_in_d2() -> None:
    """Same target-tokens interpolation on the D2 path."""
    prompt = build_condensed_prompt(text="x", target_tokens=999, depth=2)
    assert "Target length: about 999 tokens." in prompt


def test_condensed_prompt_target_tokens_interpolated_in_d3_plus() -> None:
    """Same target-tokens interpolation on the D3+ path."""
    prompt = build_condensed_prompt(text="x", target_tokens=777, depth=4)
    assert "Target length: about 777 tokens." in prompt


def test_condensed_prompt_d1_timeline_directive_is_hour_or_half_hour() -> None:
    """The TS source distinguishes timeline granularity by depth:
    D1 says "hour or half-hour", D2 says "dates and approximate time
    of day", D3+ says "date ranges". Pin the per-depth wording so a
    refactor cannot accidentally swap the granularity.
    """
    prompt = build_condensed_prompt(text="x", target_tokens=400, depth=1)
    assert (
        "Include a timeline with timestamps (hour or half-hour) for significant events." in prompt
    )


def test_condensed_prompt_d2_timeline_directive_is_dates_and_time_of_day() -> None:
    """D2 timeline granularity invariant."""
    prompt = build_condensed_prompt(text="x", target_tokens=400, depth=2)
    assert "Include a timeline with dates and approximate time of day for key milestones." in prompt


def test_condensed_prompt_d3_plus_timeline_directive_is_date_ranges() -> None:
    """D3+ timeline granularity invariant."""
    prompt = build_condensed_prompt(text="x", target_tokens=400, depth=3)
    assert "Include a brief timeline with dates (or date ranges) for major milestones." in prompt


def test_condensed_prompt_conversation_to_condense_xml_present() -> None:
    """All three condensed templates wrap the source text in the
    ``<conversation_to_condense>`` XML tag (distinct from the leaf
    template's ``<conversation_segment>``). Pin presence at every
    depth."""
    for depth in (0, 1, 2, 3, 5):
        prompt = build_condensed_prompt(
            text="SRC",
            target_tokens=400,
            depth=depth,
        )
        assert "<conversation_to_condense>\nSRC\n</conversation_to_condense>" in prompt, (
            f"missing conversation_to_condense wrapper at depth={depth}"
        )


def test_condensed_prompt_custom_instructions_routed_through_all_depths() -> None:
    """Custom instructions are forwarded to every depth's template;
    the substituted block reads "Operator instructions:\\n{text}"
    identically across D1 / D2 / D3+. Pin per-depth substitution."""
    for depth in (0, 1, 2, 3):
        prompt = build_condensed_prompt(
            text="x",
            target_tokens=400,
            depth=depth,
            custom_instructions="Custom guidance.",
        )
        assert "Operator instructions:\nCustom guidance." in prompt, (
            f"custom_instructions not substituted at depth={depth}"
        )


# ---------------------------------------------------------------------------
# build_deterministic_fallback — Wave-4 P0 + Wave-9 invariants
# ---------------------------------------------------------------------------


def test_fallback_marker_uses_em_dash_not_hyphen() -> None:
    """The fallback marker uses an em dash (``—``, U+2014) — NOT the
    ASCII hyphen-minus (``-``). Per the porting guide §"Deterministic
    fallback", this is load-bearing: operators grep for the exact
    marker string in /lcm health output, and ``[LCM fallback summary
    -`` (hyphen) would silently miss matches against the canonical
    ``[LCM fallback summary —`` (em dash).
    """
    assert "—" in _FALLBACK_MARKER
    assert "—" in _FALLBACK_MARKER_TRUNC
    # Sanity: ASCII hyphen between "summary" and "model" would be wrong.
    assert "summary - model" not in _FALLBACK_MARKER
    assert "summary - model" not in _FALLBACK_MARKER_TRUNC


def test_fallback_marker_snapshot() -> None:
    """SHA-256 snapshot of the preserved-verbatim marker text.

    TS source: ``lossless-claw/src/summarize.ts:1091-1092``
    (``FALLBACK_MARKER``, commit ``1f07fbd``). Catches silent drift
    in the em-dash codepoint or any wording tweak."""
    expected = "935119102e36b60708ffa79e3b36a22b6699d4443846083598c6fde6ea781a74"
    actual = _sha256(_FALLBACK_MARKER)
    assert actual == expected, (
        f"_FALLBACK_MARKER drifted from TS pin (commit 1f07fbd). "
        f"actual={actual}, expected={expected}."
    )


def test_fallback_marker_trunc_snapshot() -> None:
    """SHA-256 snapshot of the truncated-source marker text.

    TS source: ``lossless-claw/src/summarize.ts:1093-1094``
    (``FALLBACK_MARKER_TRUNC``, commit ``1f07fbd``).
    """
    expected = "2449cb9f5cfd7809de41b918521069919ad9a285fc6bdeef8d5bceadfaa8d9af"
    actual = _sha256(_FALLBACK_MARKER_TRUNC)
    assert actual == expected, (
        f"_FALLBACK_MARKER_TRUNC drifted from TS pin (commit 1f07fbd). "
        f"actual={actual}, expected={expected}."
    )


def test_fallback_markers_are_distinct() -> None:
    """Wave-9 invariant: the two markers (preserved-verbatim vs
    truncated) must be distinct strings so operators reading /lcm
    health, doctor scans, or eval reports can tell at a glance whether
    truncation also occurred during the fallback path. Pin distinctness
    so a future refactor cannot collapse them.
    """
    assert _FALLBACK_MARKER != _FALLBACK_MARKER_TRUNC


def test_fallback_short_text_snapshot() -> None:
    """SHA-256 snapshot of the canonical short-text fallback output.

    Canonical fixture: ``text="hello"``, ``target_tokens=600``. The
    source fits well within ``max_chars = max(256, 600*4) = 2400``,
    so this exercises the preserved-verbatim branch. Wave-4 P0
    invariant: the marker is present even though the text fits.

    Hash cross-verified against the TS implementation via
    ``esbuild``-transpiled run.
    """
    expected = "e021cd9d16671b360ee29cc46e13d1e0e06a00a30beca3ba627359501f1fdc4a"
    fb = build_deterministic_fallback("hello", target_tokens=600)
    actual = _sha256(fb)
    assert actual == expected, (
        f"build_deterministic_fallback(short-text) drifted from TS pin "
        f"(commit 1f07fbd). actual={actual}, expected={expected}."
    )


def test_fallback_long_text_snapshot() -> None:
    """SHA-256 snapshot of the canonical truncated fallback output.

    Canonical fixture: ``text = "x" * 2000``, ``target_tokens=10``.
    With the 256 floor in effect (``max(256, 10*4) = 256``), the
    output is the truncation-marker followed by the first 256 source
    chars. Hash cross-verified against the TS implementation.
    """
    expected = "cffc4793dfaec9e31284cbe0bbb9d622b33f6a155680f61c92224459c6cfaf0c"
    fb = build_deterministic_fallback("x" * 2000, target_tokens=10)
    actual = _sha256(fb)
    assert actual == expected, (
        f"build_deterministic_fallback(truncated) drifted from TS pin "
        f"(commit 1f07fbd). actual={actual}, expected={expected}."
    )


def test_fallback_marker_always_present_even_for_short_text() -> None:
    """LCM Wave-4 P0 invariant
    (``docs/adr/029-wave-fix-provenance.md``): the fallback ALWAYS
    carries a marker, even when the source text is short enough to
    fit within the budget. Without the marker, operators cannot
    distinguish "LLM down, fallback shipped raw content" from "LLM
    ran cleanly, summary IS the source".
    """
    fb = build_deterministic_fallback("a short body", target_tokens=600)
    assert fb.startswith(_FALLBACK_MARKER + "\n"), (
        f"fallback output missing Wave-4 marker on short-text branch: {fb!r}"
    )
    assert "a short body" in fb


def test_fallback_marker_preserved_verbatim_below_short_text() -> None:
    """The preserved-verbatim branch returns ``"<marker>\\n<source>"``.
    The source text is preserved exactly (after trim) — no truncation,
    no transformation."""
    fb = build_deterministic_fallback("preserved content", target_tokens=600)
    expected_body = "preserved content"
    assert fb == f"{_FALLBACK_MARKER}\n{expected_body}"


def test_fallback_truncates_at_max_chars_for_long_text() -> None:
    """When ``len(trimmed) > max_chars``, the truncated branch returns
    ``"<marker_trunc>\\n<text[:max_chars]>"``. Source longer than
    ``target_tokens*4`` (above the 256 floor) → truncation kicks in.
    Pin: the body is exactly ``max_chars`` long, the marker is the
    truncation variant.
    """
    target_tokens = 100  # max_chars = max(256, 400) = 400
    text = "y" * 1000
    fb = build_deterministic_fallback(text, target_tokens=target_tokens)
    assert fb.startswith(_FALLBACK_MARKER_TRUNC + "\n")
    body = fb[len(_FALLBACK_MARKER_TRUNC) + 1 :]
    assert len(body) == 400
    assert body == "y" * 400


def test_fallback_floor_of_256_when_target_tokens_small() -> None:
    """The ``max(256, target_tokens * 4)`` floor protects very small
    target-token values. With ``target_tokens=10`` the naive
    multiplication would give 40 — but the floor keeps ``max_chars =
    256``. Pin the floor so a future refactor cannot drop it.
    """
    text = "z" * 1000
    fb = build_deterministic_fallback(text, target_tokens=10)
    # The output is the truncation-marker plus a body capped at 256.
    body = fb[len(_FALLBACK_MARKER_TRUNC) + 1 :]
    assert len(body) == 256, (
        f"256 floor not enforced — body length {len(body)} for target_tokens=10"
    )


def test_fallback_floor_does_not_override_larger_target_tokens() -> None:
    """``max_chars`` is the GREATER of 256 and ``target_tokens * 4`` —
    when ``target_tokens * 4 > 256`` the larger value wins. Pin so a
    future refactor cannot accidentally clamp at 256.
    """
    text = "p" * 1000
    fb = build_deterministic_fallback(text, target_tokens=200)
    body = fb[len(_FALLBACK_MARKER_TRUNC) + 1 :]
    assert len(body) == 800  # target_tokens * 4 = 800 > 256


def test_fallback_boundary_at_exactly_max_chars() -> None:
    """When the source length is EXACTLY ``max_chars`` the
    preserved-verbatim branch fires (``<=`` not ``<`` in the TS guard
    at ``summarize.ts:1097``). Pin the boundary.
    """
    target_tokens = 100  # max_chars = 400
    text = "b" * 400
    fb = build_deterministic_fallback(text, target_tokens=target_tokens)
    assert fb.startswith(_FALLBACK_MARKER + "\n")  # preserved, NOT truncated
    body = fb[len(_FALLBACK_MARKER) + 1 :]
    assert body == "b" * 400


def test_fallback_empty_string_returns_empty() -> None:
    """The TS short-circuit at ``summarize.ts:1078`` returns ``""`` for
    empty (or whitespace-only after trim) input — no marker, no body.
    """
    assert build_deterministic_fallback("", target_tokens=600) == ""


def test_fallback_whitespace_only_returns_empty() -> None:
    """Whitespace-only input also short-circuits to ``""`` — the TS
    ``if (!trimmed)`` guard fires for both empty and whitespace-only
    inputs."""
    assert build_deterministic_fallback("   \n\t  ", target_tokens=600) == ""


def test_fallback_trims_source_before_substitution() -> None:
    """``text.trim()`` runs before the length check and substitution
    — leading/trailing whitespace from the caller is stripped so the
    fallback body is the cleaned form."""
    fb = build_deterministic_fallback("  hello world  ", target_tokens=600)
    assert fb == f"{_FALLBACK_MARKER}\nhello world"


# ---------------------------------------------------------------------------
# Wave-N provenance comment audit (ADR-029)
# ---------------------------------------------------------------------------


def test_wave_4_and_wave_9_provenance_comments_present_in_source() -> None:
    """Per ADR-029, every Wave-N-load-bearing fix carries an inline
    ``# LCM Wave-N (YYYY-MM-DD): ...`` comment in the source.

    For :mod:`lossless_hermes.summarize`, the load-bearing fixes are
    the Wave-4 P0 marker-always-present invariant and the Wave-9
    distinct-marker invariant. Pin both as comment-substring matches
    on the module source so a refactor that accidentally drops the
    provenance surfaces here."""
    import pathlib

    import lossless_hermes.summarize as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    assert "LCM Wave-4" in src, (
        "Wave-4 (deterministic-fallback marker-always-present) provenance "
        "comment missing from src/lossless_hermes/summarize.py — ADR-029 "
        "requires inline `# LCM Wave-4 (...)` at the fix site."
    )
    assert "LCM Wave-9" in src, (
        "Wave-9 (distinct preserved-verbatim vs truncated markers) "
        "provenance comment missing from src/lossless_hermes/summarize.py "
        "— ADR-029 requires inline `# LCM Wave-9 (...)` at the fix site."
    )
