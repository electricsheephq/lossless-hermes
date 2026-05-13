#!/usr/bin/env python3
"""Strict-subset schema-diff verifier for the Epic 01 transition.

Used by ``./scripts/schema_diff.sh --verify-subset``. Compares a Python-
extracted schema dump (from ``extract_python_schema.py``) against the
committed TS-extracted golden reference (``tests/fixtures/lcm_reference_schema.sql``)
and exits with one of:

* **0** — Python schema is a strict subset of the reference. Every Python
  object exists in the reference with matching DDL (modulo whitespace).
  Reference may have extra objects (deferred to later Epic 01 issues
  #01-05 / #01-06 / #01-15).
* **1** — Schema mismatch: at least one Python object's DDL diverges from
  the reference's DDL (real drift requiring fix).
* **5** — Forbidden: Python created at least one object the reference does
  not have. This is true schema drift; either the Python migration is
  wrong, or the reference is stale and needs ``--refresh-reference``.

Why this mode exists: during Epic 01 ramp-up, issues #01-04 / #01-05 /
#01-06 / #01-15 each contribute a chunk of the full schema. Each individual
PR's Python output is a strict subset of the reference. The full ``--verify``
mode would exit 1 (drift) on each interim PR; this mode lets CI gate that
"every Python object matches, none extra" without blocking on the missing
objects scheduled for later PRs.

Output format:

* On success — stderr: ``✅ Python schema is a strict subset of TS reference``
  followed by the count of matching objects and the count of reference-
  only (deferred) objects.
* On mismatch — stderr: detailed list of which objects diverged.
* On forbidden — stderr: detailed list of Python-only objects.

Usage::

    python3 scripts/schema_subset_check.py \\
        --reference tests/fixtures/lcm_reference_schema.sql \\
        --python /tmp/py_schema.sql
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable


def _parse_schema_dump(text: str) -> dict[str, tuple[str, str]]:
    """Parse a schema dump into {name: (type, normalized_sql)}.

    Both ``extract_python_schema.py`` and ``extract_lcm_schema.ts`` produce
    the same format::

        -- <type>: <name>
        <CREATE statement>;

        -- <type>: <name>
        ...

        -- pragmas
        ...

    Returns a dict keyed on object name; collisions between table and
    index of the same name are not possible in SQLite.
    """
    objects: dict[str, tuple[str, str]] = {}
    current_type: str | None = None
    current_name: str | None = None
    current_sql_lines: list[str] = []

    def _flush() -> None:
        nonlocal current_name, current_type, current_sql_lines
        if current_name and current_type and current_sql_lines:
            raw_sql = "\n".join(current_sql_lines).strip().rstrip(";").strip()
            objects[current_name] = (current_type, _normalize_sql(raw_sql))
        current_name = None
        current_type = None
        current_sql_lines = []

    for line in text.splitlines():
        header_match = re.match(r"^-- (table|index|trigger|view): (\S+)\s*$", line)
        if header_match:
            _flush()
            current_type = header_match.group(1)
            current_name = header_match.group(2)
            continue
        if line.strip() == "-- pragmas":
            _flush()
            break
        if current_name is not None and not line.startswith("--"):
            current_sql_lines.append(line)

    _flush()
    return objects


def _normalize_sql(sql: str) -> str:
    """Collapse whitespace + lower-case keywords for byte-loose comparison."""
    return re.sub(r"\s+", " ", sql).strip().lower()


def _bulleted(items: Iterable[str]) -> str:
    """Return a bullet list of items, one per line."""
    return "\n".join(f"  - {item}" for item in items)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    args = parser.parse_args()

    ref = _parse_schema_dump(args.reference.read_text(encoding="utf-8"))
    py = _parse_schema_dump(args.python.read_text(encoding="utf-8"))

    ref_names = set(ref.keys())
    py_names = set(py.keys())

    py_only = py_names - ref_names
    if py_only:
        sys.stderr.write(
            "❌ Forbidden: Python schema contains objects the reference does NOT have.\n"
            "   Either the Python migration emitted unexpected DDL, or the\n"
            "   committed reference is stale and needs `--refresh-reference`.\n"
            f"   Python-only objects ({len(py_only)}):\n{_bulleted(sorted(py_only))}\n"
        )
        return 5

    mismatches: list[str] = []
    for name in sorted(py_names):
        py_type, py_sql = py[name]
        ref_type, ref_sql = ref[name]
        if py_type != ref_type:
            mismatches.append(f"{name}: type mismatch (py={py_type}, ref={ref_type})")
        elif py_sql != ref_sql:
            mismatches.append(f"{name}: DDL differs\n     py:  {py_sql}\n     ref: {ref_sql}")

    if mismatches:
        sys.stderr.write(
            f"❌ Schema mismatch on {len(mismatches)} object(s):\n{_bulleted(mismatches)}\n"
        )
        return 1

    deferred = ref_names - py_names
    sys.stderr.write(
        f"✅ Python schema is a strict subset of TS reference.\n"
        f"   Matched: {len(py_names)} object(s).\n"
        f"   Deferred to later Epic 01 issues: {len(deferred)} object(s).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
