"""Compare loaded Python schemas to the committed JSON-Schema export.

Per [ADR-016 §Consequences](../../docs/adr/016-typebox-translation.md):

> A CI test (``tests/tools/test_schemas_match_ts.py``) compares the
> loaded Python schemas to a JSON-Schema export from the TS source
> (run once via a ``tsc + node`` step in the LCM repo and committed as
> a fixture under ``tests/fixtures/lcm_v4.1_schemas.json``). Catches
> drift if the TS source moves while we sleep.

The matching test is **xfail** until the LCM-side export tooling
ships. Per
[issue 06-01 §"5% uncertainty"](../../epics/06-tools/06-01-typebox-translation-conventions.md):

> the LCM-side fixture-export tooling doesn't exist yet (someone has
> to write the ``tsc + node`` step that emits
> ``lcm_v4.1_schemas.json``). Can be deferred to a follow-up if Wave A
> ships before the fixture exists; in that case the schema match test
> is ``xfail`` until the fixture lands.

When the fixture lands:

1. Drop the fixture at ``tests/fixtures/lcm_v4.1_schemas.json``. The
   version segment is in the filename so version skew is obvious in
   diffs (ADR-016 §Open-questions row 1).

2. Remove the ``@pytest.mark.xfail`` decorators below. The test
   parametrizes over :func:`get_tool_schemas` and asserts byte-identical
   match against the fixture entry keyed by tool name.

3. Drift is fixed by **deliberately re-porting** the description
   string in the Python source — not by relaxing the test. The whole
   point of byte-identity is that a Wave-13+ TS prose tweak surfaces
   here.

Source: lossless-claw at commit ``1f07fbd``, branch ``pr-613``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lossless_hermes.tools import get_tool_schemas

# ---------------------------------------------------------------------------
# Fixture location
# ---------------------------------------------------------------------------
#
# The fixture is keyed by LCM version, not by commit SHA — version skew
# is more readable in diffs (a v4.1 -> v4.2 bump is more informative
# than two opaque SHAs). ADR-016 §Open-questions row 1 names the file.

_FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "lcm_v4.1_schemas.json"
_FIXTURE_MISSING_REASON = (
    "lcm_v4.1_schemas.json fixture not yet emitted by the LCM build — see "
    "docs/typebox-translation.md §Fixture comparison (deferred until LCM "
    "export ships). Issue 06-01 §5% uncertainty defers this test to a "
    "follow-up when the tsc+node export tooling lands."
)


def _load_fixture() -> dict[str, Any] | None:
    """Load the TS-exported schema fixture, or ``None`` if absent.

    Returning ``None`` lets the parametrized test decorate ``xfail``
    with a precise ``condition`` instead of crashing during test
    collection — important because the fixture may be added in a
    different PR than this one.
    """
    if not _FIXTURE_PATH.exists():
        return None
    with _FIXTURE_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


_FIXTURE = _load_fixture()


# ---------------------------------------------------------------------------
# Schema-by-schema comparison
# ---------------------------------------------------------------------------


def _schema_id(schema: dict[str, Any]) -> str:
    name = schema.get("name")
    return name if isinstance(name, str) else "unnamed"


@pytest.mark.xfail(
    condition=_FIXTURE is None,
    reason=_FIXTURE_MISSING_REASON,
    strict=False,
)
@pytest.mark.parametrize("schema", get_tool_schemas(), ids=_schema_id)
def test_schema_matches_ts_export(schema: dict[str, Any]) -> None:
    """Each Python schema is byte-identical to the TS-exported fixture.

    Per ADR-016 §Consequences — the comparison is byte-identical (no
    whitespace normalization) so a Wave-13+ TS prose tweak surfaces as
    a failing test on the next bump.

    Notes:
        Skipped (via ``xfail``) until the fixture lands. The xfail is
        ``strict=False`` because the registry is empty at issue 06-01
        — a passing test on an empty parametrization is a no-op, not
        a real pass.
    """
    if _FIXTURE is None:  # pragma: no cover — xfail handles this path
        pytest.xfail(_FIXTURE_MISSING_REASON)

    name = schema["name"]
    assert name in _FIXTURE, (
        f"Tool {name!r} is not in the TS-exported fixture. "
        f"Either the tool name drifted (rename it back to match) or "
        f"the fixture is out-of-date (re-run the LCM export step)."
    )
    expected = _FIXTURE[name]
    assert schema == expected, (
        f"Tool {name!r} schema differs from the TS export. "
        f"Re-port the description prose from the TS source verbatim — "
        f"DO NOT relax this test. See docs/typebox-translation.md "
        f"§'Verbatim-description rule' for the policy."
    )


# ---------------------------------------------------------------------------
# Fixture coverage — every fixture entry has a corresponding Python schema
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    condition=_FIXTURE is None,
    reason=_FIXTURE_MISSING_REASON,
    strict=False,
)
def test_fixture_has_no_orphan_entries() -> None:
    """Every fixture entry corresponds to a registered Python tool schema.

    If the TS source declared a tool the Python port hasn't covered
    yet, this test fails — that's the signal to open the next per-tool
    porting issue.
    """
    if _FIXTURE is None:  # pragma: no cover — xfail handles this path
        pytest.xfail(_FIXTURE_MISSING_REASON)

    python_names = {s["name"] for s in get_tool_schemas()}
    fixture_names = set(_FIXTURE.keys())
    orphans = fixture_names - python_names
    assert not orphans, (
        f"The TS-exported fixture has {len(orphans)} tool(s) "
        f"without a Python port: {sorted(orphans)}. "
        f"Open a port issue (06-07..06-14) for each."
    )
