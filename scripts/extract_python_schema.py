#!/usr/bin/env python3
"""
Extract Python lossless_hermes SQLite schema by running run_lcm_migrations().

This is the verify-side counterpart to extract_lcm_schema.ts. It runs the
Python migration ladder against an in-memory SQLite DB and dumps the resulting
schema in the same format so a textual diff against the TS reference is meaningful.

Usage:
    python3 scripts/extract_python_schema.py --output /tmp/py_schema.sql

Or via the orchestrator:
    ./scripts/schema_diff.sh --verify

Status (2026-05-13): This script is a Wave 0 SCAFFOLD. Python migrations are
delivered by Epic 01 (Wave 2). Until then this script intentionally errors with
a clear message — that's expected behavior in Wave 0 / Wave 1.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone


def extract_schema(conn: sqlite3.Connection) -> str:
    """Return SQL DDL one statement per logical block, ordered (type, name)."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT type, name, tbl_name, sql
          FROM sqlite_master
         WHERE sql IS NOT NULL
           AND name NOT LIKE 'sqlite_%'
         ORDER BY type, name
        """
    )
    rows = cur.fetchall()

    blocks: list[str] = []
    blocks.append("-- lossless-hermes Python schema")
    blocks.append(f"-- Generated: {datetime.now(timezone.utc).isoformat()}")
    blocks.append(f"-- Total objects: {len(rows)}")
    blocks.append("")

    for row in rows:
        type_, name, _tbl_name, sql = row
        blocks.append(f"-- {type_}: {name}")
        # Trim trailing whitespace, ensure terminating semicolon.
        sql_normalized = sql.strip().rstrip(";") + ";"
        blocks.append(sql_normalized)
        blocks.append("")

    blocks.append("-- pragmas")
    for pragma in ("foreign_keys", "journal_mode", "synchronous", "user_version"):
        cur.execute(f"PRAGMA {pragma}")
        result = cur.fetchone()
        blocks.append(f"-- pragma {pragma}: {result}")
    blocks.append("")

    return "\n".join(blocks)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", help="Path to write schema dump. If omitted, prints to stdout.")
    args = parser.parse_args()

    # Try to import Python migrations.
    try:
        from lossless_hermes.db.migration import run_lcm_migrations  # type: ignore
    except ImportError:
        print(
            "extract_python_schema: lossless_hermes.db.migration.run_lcm_migrations not yet available.\n"
            "This is expected in Wave 0 / Wave 1 before Epic 01 (Storage) ships. The schema-diff CI\n"
            "scaffold is in place; the verify-side check becomes meaningful as Epic 01 issues land.\n"
            "\n"
            "If you're past Wave 2 and seeing this error, the package is not installed: `pip install -e .`",
            file=sys.stderr,
        )
        return 2  # distinct exit code so CI can treat "not yet implemented" differently

    conn = sqlite3.connect(":memory:")
    run_lcm_migrations(conn, fts5_available=True, seed_default_prompts=True)
    output = extract_schema(conn)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"wrote schema to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(output)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
