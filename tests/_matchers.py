"""Asymmetric matchers for porting vitest's expect.any / expect.objectContaining.

Each class implements __eq__ so it compares true against any value matching the
declared shape. Use them in equality assertions:

    assert actual == {"role": "user", "ts": AnyOf(int), "msg": ContainsString("hello")}

This replaces vitest's:

    expect(actual).toEqual({ role: "user", ts: expect.any(Number),
                            msg: expect.stringContaining("hello") })

See ADR-028 §"Decision" point 6. Source-of-truth for the class shapes is
`docs/adr/028-vitest-to-pytest.md` lines 95-154 — kept verbatim so a future
audit can grep both files for parity.

Usage counts from the TS suite (per ADR-028 §Context and the porting guide
§"Asymmetric matchers"):

    AnyOf            replaces expect.any(...)               — 73x in TS
    ContainsObject   replaces expect.objectContaining({...}) — 82x in TS
    ContainsString   replaces expect.stringContaining("x")   — 53x in TS
    ContainsArray    replaces expect.arrayContaining([...])  — 9x in TS
    MatchesString    replaces expect.stringMatching(/x/)     — 4x in TS

The 100+ high-frequency cases (AnyOf, ContainsObject, ContainsString) are the
load-bearing ones for the port.
"""

from __future__ import annotations

import re
from typing import Any, Type


class AnyOf:
    """Matches any value of the given type. Replaces vitest's expect.any(Cls)."""

    def __init__(self, cls: Type[Any]) -> None:
        self._cls = cls

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, self._cls)

    def __repr__(self) -> str:
        return f"AnyOf({self._cls.__name__})"


class ContainsObject:
    """Subset-equality for dicts. Replaces vitest's expect.objectContaining({...})."""

    def __init__(self, expected: dict) -> None:
        self._expected = expected

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, dict):
            return False
        return all(other.get(k) == v for k, v in self._expected.items())

    def __repr__(self) -> str:
        return f"ContainsObject({self._expected!r})"


class ContainsString:
    """Substring-match. Replaces vitest's expect.stringContaining("...")."""

    def __init__(self, substr: str) -> None:
        self._substr = substr

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, str) and self._substr in other

    def __repr__(self) -> str:
        return f"ContainsString({self._substr!r})"


class ContainsArray:
    """Subset-match for lists. Replaces vitest's expect.arrayContaining([...])."""

    def __init__(self, expected: list) -> None:
        self._expected = expected

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, list):
            return False
        return all(item in other for item in self._expected)

    def __repr__(self) -> str:
        return f"ContainsArray({self._expected!r})"


class MatchesString:
    """Regex-match. Replaces vitest's expect.stringMatching(/x/)."""

    def __init__(self, pattern: str) -> None:
        self._pattern = re.compile(pattern)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, str) and bool(self._pattern.search(other))

    def __repr__(self) -> str:
        return f"MatchesString({self._pattern.pattern!r})"
