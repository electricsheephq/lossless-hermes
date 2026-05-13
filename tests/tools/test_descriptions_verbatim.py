"""Byte-identical verbatim lint for every LCM tool ``description`` string.

Loads :func:`lossless_hermes.tools.get_tool_schemas` and asserts each
registered tool's ``description`` field is byte-identical to the
corresponding entry in
:data:`tests/fixtures/lcm_v4.1_tool_descriptions.json`. The fixture is
extracted ONCE from the pinned LCM TS source (commit ``1f07fbd``,
branch ``pr-613``) and re-pinned whenever the source-map bumps —
together with the Python-side re-port.

Why a separate test
-------------------

There is overlap with ``test_schemas_match_ts.py`` (the future
full-schema comparison) but the **description string** is the highest-
risk drift surface:

* The description prose is what the model reads. Whitespace,
  punctuation, or wording drift silently degrades tool-selection
  behavior — exactly the failure mode ADR-016 names.
* Wave-1 -> Wave-12 audits tuned these strings for PRIMARY-vs-secondary
  routing hints, fallback suggestions, env-var names, parameter caps,
  and Wave-12 partial-success annotations.
* A converter or paraphrasing pass would normalize whitespace, re-
  escape quotes, or rewrap the prose — all silent. The byte-identical
  check catches every such drift.

Per [ADR-016 §"Verbatim-description rule"](../../docs/adr/016-typebox-translation.md):

> Description strings are pasted verbatim from ``tools.md``, which
> itself was lifted verbatim from the TS source.

And per [issue 06-15](../../epics/06-tools/06-15-tool-descriptions-verbatim.md):

> A converter or paraphrasing pass silently destroys this. ADR-016
> codifies hand-translation; this test pins the result.

Test taxonomy
-------------

* :func:`test_every_registered_tool_description_matches_fixture` —
  for each registered tool, asserts ``schema["description"] ==
  fixture[name]`` byte-identical. Failure prints a ``difflib.unified_diff``
  so the reviewer can read the prose-level delta.
* :func:`test_no_extra_tools_registered` — the set of registered
  names is a subset of the v0.1.0 expected set (``lcm_grep``,
  ``lcm_describe``, ``lcm_expand``, ``lcm_synthesize_around``,
  ``lcm_get_entity``, ``lcm_search_entities``, ``lcm_compact``).
  ``lcm_expand_query`` is in the fixture for completeness but the
  v0.1.0 test skips its registration (ADR-012 defers the sub-agent).
* :func:`test_no_missing_tools_registered` — the set of expected v0.1.0
  tools is a subset of the registered set. Together with the
  no-extras assertion, this pins the registry to the exact 7-tool
  v0.1.0 surface. Skipped (``xfail``) while per-tool ports
  (06-07..06-14) are in flight; flips to ``strict`` when the v0.1.0
  registry stabilizes.
* :func:`test_fixture_provenance_matches_source_pin` — the
  ``_provenance`` field in the fixture must match the canonical LCM
  source pin recorded in
  ``docs/reference/lcm-source-map.md``. So when LCM bumps, the fixture
  must be regenerated AND re-pinned in the same PR — the test fails
  loudly otherwise.
* :func:`test_description_sha256_snapshots` — per-tool SHA-256 lock on
  the fixture's description bytes. Pattern mirrors
  ``tests/test_recall_policy.py`` (#39) and
  ``tests/test_summarize_prompts.py`` (#85). Catches fixture-side
  drift if anyone hand-edits the JSON without re-running the extractor.

Regenerating the fixture
------------------------

See ``scripts/extract_tool_descriptions.mjs`` (Node) — runs against
the pinned LCM tree, emits the JSON used here. Bump the
``_provenance`` line in the same PR that bumps
``docs/reference/lcm-source-map.md`` and re-port any drifted
description into the matching Python source file.

References
----------

* ADR-016 — TypeBox -> JSON Schema hand-translation policy.
* ADR-029 — Wave-N provenance comments (these descriptions carry
  Wave-12 model-facing annotations).
* `docs/porting-guides/tools.md`:33 — "every ``description`` string
  below is **verbatim from the TS source** — these are load-bearing
  for the model."
* Issue 06-15 — this test's spec.

Source: lossless-claw at commit ``1f07fbd``, branch ``pr-613``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from lossless_hermes.tools import get_tool_schemas

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------
#
# The fixture is keyed by LCM version (``v4.1``) in the filename — version
# skew is more readable in diffs than two opaque SHAs. The ``_provenance``
# field inside the JSON carries the precise commit SHA, cross-checked by
# :func:`test_fixture_provenance_matches_source_pin` below.

_FIXTURE_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "fixtures" / "lcm_v4.1_tool_descriptions.json"
)


def _load_fixture() -> dict[str, Any]:
    """Load the verbatim-description fixture as a JSON dict.

    Raises:
        FileNotFoundError: If the fixture is missing. (The fixture is
            committed in this PR; absence means a repository corruption
            or accidental delete — fail loudly rather than xfail.)
    """
    with _FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


_FIXTURE: Final[dict[str, Any]] = _load_fixture()

# v0.1.0 ships seven tools. ``lcm_expand_query`` is in the fixture (for
# parity with the TS surface and to make future un-deferral mechanical)
# but the test set excludes it — per ADR-012 the sub-agent is deferred.
_V01_TOOL_NAMES: Final[frozenset[str]] = frozenset({
    "lcm_grep",
    "lcm_describe",
    "lcm_expand",
    "lcm_synthesize_around",
    "lcm_get_entity",
    "lcm_search_entities",
    "lcm_compact",
})


def _registered_tool_names() -> set[str]:
    """Set of tool names currently registered in :data:`TOOL_SCHEMAS`.

    Wraps :func:`get_tool_schemas` for readability — the schemas list
    is hot-path stable but we want a set for membership math.
    """
    return {s["name"] for s in get_tool_schemas()}


# ---------------------------------------------------------------------------
# Source-pin provenance — bumping LCM forces a fixture regeneration
# ---------------------------------------------------------------------------


_SOURCE_MAP_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "reference" / "lcm-source-map.md"
)


def _read_pinned_lcm_commit() -> str:
    """Extract the canonical LCM commit SHA from the source-map doc.

    The doc's first ``**Source:**`` line has shape::

        **Source:** ``<path>`` @ branch ``pr-613``, commit ``<40-hex>``

    Returns the 40-hex SHA. Raises ``AssertionError`` if the line can't
    be parsed — a defensive guard so a doc edit that breaks the SHA
    parser fails this test deliberately rather than silently degrading
    the provenance check.
    """
    text = _SOURCE_MAP_PATH.read_text(encoding="utf-8")
    # Match the first ``**Source:**`` line and pull the commit hex.
    m = re.search(r"\bcommit\s+`([0-9a-f]{40})`", text)
    assert m is not None, (
        f"Could not parse commit SHA from {_SOURCE_MAP_PATH}. "
        f"The first-line `**Source:**` template likely changed; "
        f"update this test's regex or the doc's template."
    )
    return m.group(1)


def test_fixture_provenance_matches_source_pin() -> None:
    """The fixture's ``_provenance`` matches the source-map pin.

    Bumping LCM is a coordinated change: a PR that bumps the
    source-map SHA but forgets to re-run the description extractor
    will leave the fixture stale. This assertion fails loudly in that
    case so the operator regenerates the fixture in the same PR.

    Per [ADR-016 §"Open questions / 5% uncertainty"](../../docs/adr/016-typebox-translation.md):

    > include the LCM-source-map version pin (``pr-613@1f07fbd``) in
    > the fixture filename so version skew is obvious in diffs.
    """
    pinned = _read_pinned_lcm_commit()
    expected = f"lossless-claw@{pinned}"
    actual = _FIXTURE.get("_provenance")
    assert actual == expected, (
        f"Fixture provenance drifted from the pinned LCM source. "
        f"actual={actual!r}, expected={expected!r}. "
        f"Regenerate via scripts/extract_tool_descriptions.mjs at the "
        f"current LCM pin and re-port any prose drift in the matching "
        f"src/lossless_hermes/tools/*.py module."
    )


# ---------------------------------------------------------------------------
# Description byte-identity — the main lint
# ---------------------------------------------------------------------------


def _diff_message(name: str, actual: str, expected: str) -> str:
    """Render a difflib.unified_diff message for a description mismatch.

    Description strings are model-facing prose, so the unified diff is
    the friendliest format for a reviewer to read the delta.

    Per issue 06-15 AC:

    > Fail message includes the diff between expected and actual (use
    > ``difflib.unified_diff``).
    """
    # ``splitlines(keepends=True)`` lets the diff align line-by-line.
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"fixture[{name}]",
            tofile=f"registered[{name}]",
            n=1,
        )
    )
    return (
        f"Tool {name!r} description drifted from the verbatim TS fixture. "
        f"Re-port the literal from lossless-claw source — DO NOT relax this "
        f"test. ADR-016 codifies hand-translation; this lint is the "
        f"backstop.\n\n"
        f"Unified diff (expected = fixture, actual = registered):\n{diff}"
    )


@pytest.mark.parametrize(
    "schema",
    get_tool_schemas(),
    ids=lambda s: s["name"] if isinstance(s.get("name"), str) else "unnamed",
)
def test_every_registered_tool_description_matches_fixture(
    schema: dict[str, Any],
) -> None:
    """Every registered tool's ``description`` is byte-identical to the fixture.

    The test is parametrized over the live registry — as 06-07..06-14
    land, new schemas auto-extend the parametrization.

    Notes:
        Byte-identical, not whitespace-normalized. A Wave-13+ TS prose
        tweak surfaces as a deliberate test-update in this file's
        partner PR (the one that re-extracts the fixture).
    """
    name = schema["name"]
    assert name in _FIXTURE, (
        f"Tool {name!r} is not in the verbatim fixture. "
        f"Either the tool name drifted (rename it back to match the TS "
        f"source) or the fixture is out-of-date (regenerate via "
        f"scripts/extract_tool_descriptions.mjs at the current LCM pin)."
    )
    expected = _FIXTURE[name]
    actual = schema["description"]
    assert actual == expected, _diff_message(name, actual, expected)


# ---------------------------------------------------------------------------
# Registry-set assertions — pin the v0.1.0 tool surface
# ---------------------------------------------------------------------------


def test_no_extra_tools_registered() -> None:
    """No tool outside the v0.1.0 expected set is registered.

    Per [ADR-012](../../docs/adr/012-subagent-defer.md), ``lcm_expand_query``
    is deferred — it's in the fixture (for parity) but must NOT be
    registered in v0.1.0. Catches accidental registration that would
    expose the sub-agent to the model.

    A passing instance of this test when the registry is empty (Wave 5
    in flight) is informational only — the no-missing test below pins
    the lower bound.
    """
    registered = _registered_tool_names()
    extras = registered - _V01_TOOL_NAMES
    assert not extras, (
        f"Unexpected tools registered for v0.1.0: {sorted(extras)}. "
        f"Per ADR-012, the v0.1.0 surface is exactly "
        f"{sorted(_V01_TOOL_NAMES)}. If a new tool needs to ship, "
        f"update _V01_TOOL_NAMES here AND add the description to the "
        f"verbatim fixture."
    )


def test_no_missing_tools_registered() -> None:
    """All seven v0.1.0 tools are registered.

    Marked ``xfail`` while Wave-5 per-tool ports (06-07..06-14) are in
    flight. Flips to a real assert when the registry stabilizes:

    1. Remove the ``@pytest.mark.xfail`` decorator.
    2. CI is then a hard gate on the v0.1.0 release readiness.

    The xfail is ``strict=False`` so the test passes once every tool
    lands without a follow-up PR to flip the marker.
    """
    registered = _registered_tool_names()
    missing = _V01_TOOL_NAMES - registered
    if missing:
        # While per-tool ports are in flight, this is expected. Use
        # ``xfail`` so the test surfaces in CI ("xfailed: 06-08 grep
        # pending"). Flip to ``raise AssertionError`` once stable.
        pytest.xfail(
            reason=(
                f"v0.1.0 expects {len(_V01_TOOL_NAMES)} tools registered; "
                f"missing {sorted(missing)} (per-tool ports 06-07..06-14 "
                f"in flight). Remove this xfail when Wave 5 closes."
            )
        )
    # Once all 7 tools land, this assertion passes unconditionally.
    assert not missing


# ---------------------------------------------------------------------------
# SHA-256 snapshots — fixture-side drift detector
# ---------------------------------------------------------------------------
#
# Mirrors the snapshot pattern in tests/test_recall_policy.py (PR #39) and
# tests/test_summarize_prompts.py (PR #85): the description body is
# pinned to a SHA-256 so a hand-edit to the JSON without re-running the
# extractor fails this test. Together with
# :func:`test_fixture_provenance_matches_source_pin`, this gates against
# both "stale fixture" (provenance test) and "tampered fixture" (snapshot
# test) failure modes.
#
# Computed from the byte-identical TS source extraction via
# ``scripts/extract_tool_descriptions.mjs`` at commit ``1f07fbd``.


_DESCRIPTION_SHA256: Final[dict[str, str]] = {
    "lcm_grep": "4e87da8b813ef3f188000b7dc210a5f2ce3de3eefe50dec0f37eafef48a431f0",
    "lcm_describe": "c53732f75dce0bc8f617a3af8e48cdee6c81c0067c2c391c90c479d385d83d99",
    "lcm_expand": "2bc548c1da6a5563afac650741185f574a57cfca63d8d3ce748f2593808ddbb7",
    "lcm_synthesize_around": "3a45f21fff4913fddb8a37693b288bb9bc2452fc87da879f7b14dc55199b7ec2",
    "lcm_get_entity": "367d32f629a43d5cfbe7258e84b3eacc59d4fb039adbc36f1d444aeea2fc9198",
    "lcm_search_entities": "246c9f6f1d6c4a354f27e8998781700ff03063d24033d04a539b9276689b66ea",
    "lcm_compact": "8f4f484490cf06ad9d2e0b9e20fd847c6b5b05f064c1341fd28052827405bf2a",
    "lcm_expand_query": "ab77cdbb7a9e756591159e3056051405d8e639f9840e8f611ee999b18efecfe7",
}


@pytest.mark.parametrize(
    ("name", "expected_sha256"),
    sorted(_DESCRIPTION_SHA256.items()),
    ids=lambda v: v if isinstance(v, str) and v in _DESCRIPTION_SHA256 else None,
)
def test_description_sha256_snapshots(name: str, expected_sha256: str) -> None:
    """SHA-256 snapshot lock on each fixture description.

    Catches the failure mode where someone edits
    ``tests/fixtures/lcm_v4.1_tool_descriptions.json`` directly
    (e.g. fixing a "typo" they spotted) without re-running the
    extractor. The fixture is the snapshot of the TS source — edit the
    extractor and re-run, never edit by hand.

    Hash computed at port time from the TS source at commit ``1f07fbd``
    via ``scripts/extract_tool_descriptions.mjs``. If an intentional
    TS bump lands, update the expected hash here AND the fixture file
    in the same PR.
    """
    assert name in _FIXTURE, (
        f"Tool {name!r} missing from fixture — the SHA-256 table is "
        f"out of sync with the fixture keys."
    )
    actual = hashlib.sha256(_FIXTURE[name].encode("utf-8")).hexdigest()
    assert actual == expected_sha256, (
        f"Description {name!r} drifted from the SHA-256 snapshot "
        f"(commit 1f07fbd). actual={actual}, expected={expected_sha256}. "
        f"If this drift is intentional (TS pulled forward), regenerate "
        f"the fixture via scripts/extract_tool_descriptions.mjs and "
        f"update the expected hash here in the same PR. DO NOT update "
        f"only the hash without regenerating the fixture — they are a "
        f"matched pair."
    )


def test_fixture_keys_match_sha256_table() -> None:
    """Every fixture description has a SHA-256 entry and vice versa.

    Guards against drift between the JSON fixture and the SHA table —
    e.g. adding a new tool to the fixture but forgetting to add a hash.
    """
    fixture_tools = {k for k in _FIXTURE if not k.startswith("_")}
    sha_tools = set(_DESCRIPTION_SHA256.keys())
    only_in_fixture = fixture_tools - sha_tools
    only_in_sha = sha_tools - fixture_tools
    assert not only_in_fixture and not only_in_sha, (
        f"SHA-256 table is out of sync with fixture keys. "
        f"In fixture but not SHA: {sorted(only_in_fixture)}. "
        f"In SHA but not fixture: {sorted(only_in_sha)}."
    )
