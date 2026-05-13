"""Tests for :mod:`lossless_hermes.tools.entity_shared` (issue 07-01).

Ports the parity checks for ``lossless-claw/src/tools/lcm-entity-shared.ts``
(LCM commit ``1f07fbd`` on branch ``pr-613``, 84 LOC). The upstream TS
has no dedicated test file — it's exercised end-to-end by
``lcm-get-entity-tool.test.ts`` — so this Python suite is **new
coverage**, deliberately scoped to the issue 07-01 acceptance criteria.

Coverage:

* **Vendored TS-fixture parity** — three fixtures captured by running
  the literal TS template literals through ``node`` at port time
  (``tests/fixtures/entity_shared_*.txt``). The fixtures are excluded
  from the ``trailing-whitespace`` + ``end-of-file-fixer`` pre-commit
  hooks because the JS template-literal output deliberately includes
  a 6-space blank line (when ``include_first_in=False``) and lacks a
  trailing newline (the agg CTE returns from a backtick literal whose
  closing backtick sits right after ``)``).
* **Variant differentiation** — ``include_first_in=True`` must contain
  the ``first_in`` subquery; ``include_first_in=False`` must not.
* **Compose-and-prepare** — both variants concatenated with
  :data:`VISIBLE_MENTIONS_CTE` and a minimal ``SELECT`` body must parse
  as a valid SQLite prepared statement against the migrated v4.1
  schema. We don't ``execute()``-fetch rows here (the catalog is empty);
  ``sqlite3.Connection.cursor.execute`` against an empty DB validates
  parser + bind + index resolution.
* **No SQL execution at import time** — verified by importing the
  module under a fresh ``importlib`` cache and checking no DB handle
  leaks into the module namespace.

These cases mirror the issue 07-01 spec "Tests to port" table:

| Source | Cases |
|---|---|
| ``test/lcm-entity-shared.test.ts`` (~50 LOC, hypothetical sibling) |
| (1) CTE literal matches reference; |
| (2) ``entityAggCte({ includeFirstIn: true })`` includes ``first_in``; |
| (3) ``entityAggCte({ includeFirstIn: false })`` omits ``first_in``; |
| (4) both compose with ``VISIBLE_MENTIONS_CTE`` to form executable SQL |
"""

from __future__ import annotations

import importlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.tools.entity_shared import (
    VISIBLE_MENTIONS_CTE,
    entity_agg_cte,
)

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
_FIXTURE_VISIBLE = _FIXTURE_DIR / "entity_shared_visible_mentions_cte.txt"
_FIXTURE_AGG_TRUE = _FIXTURE_DIR / "entity_shared_agg_cte_with_first_in.txt"
_FIXTURE_AGG_FALSE = _FIXTURE_DIR / "entity_shared_agg_cte_no_first_in.txt"


def _read_fixture(path: Path) -> str:
    """Read a vendored TS-template fixture as text.

    ``encoding='utf-8'`` is explicit per ``PLW1514`` (the only ruff
    rule enabled in pyproject.toml). ``newline=""`` keeps CR/LF
    handling under test control — the fixtures are guaranteed LF by
    the ``mixed-line-ending --fix=lf`` pre-commit hook. We can't pass
    ``newline=`` to :meth:`pathlib.Path.read_text` (only added in 3.13
    and we still support 3.11/3.12), so we use ``open()`` directly.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Vendored TS-fixture parity
# ---------------------------------------------------------------------------


class TestVisibleMentionsCte:
    """``VISIBLE_MENTIONS_CTE`` must match the vendored TS fixture."""

    def test_byte_equals_vendored_fixture(self) -> None:
        expected = _read_fixture(_FIXTURE_VISIBLE)
        assert VISIBLE_MENTIONS_CTE == expected

    def test_length_locked(self) -> None:
        # Belt-and-suspenders next to the byte-equality test: a length
        # check makes accidental whitespace drift obvious in CI logs
        # before the diff-formatted equality failure.
        assert len(VISIBLE_MENTIONS_CTE) == 225

    def test_starts_with_leading_newline(self) -> None:
        # JS backtick literal preserves the newline right after the
        # opening backtick. Callers rely on this when concatenating
        # with the agg CTE — the leading newline keeps the WITH on
        # its own line in the rendered prepared-statement source.
        assert VISIBLE_MENTIONS_CTE.startswith("\n")

    def test_contains_suppression_filter(self) -> None:
        # The whole reason this CTE exists (Wave-12 reviewer F4 fix):
        # filter out mentions whose parent summary is suppressed.
        assert "WHERE s.suppressed_at IS NULL" in VISIBLE_MENTIONS_CTE

    def test_joins_lcm_entity_mentions_to_summaries(self) -> None:
        assert "FROM lcm_entity_mentions m" in VISIBLE_MENTIONS_CTE
        assert "JOIN summaries s ON s.summary_id = m.summary_id" in VISIBLE_MENTIONS_CTE


class TestEntityAggCteWithFirstIn:
    """``entity_agg_cte(include_first_in=True)`` — used by ``lcm_get_entity``."""

    def test_byte_equals_vendored_fixture(self) -> None:
        expected = _read_fixture(_FIXTURE_AGG_TRUE)
        assert entity_agg_cte(include_first_in=True) == expected

    def test_length_locked(self) -> None:
        assert len(entity_agg_cte(include_first_in=True)) == 503

    def test_includes_first_in_subquery(self) -> None:
        sql = entity_agg_cte(include_first_in=True)
        assert "(SELECT vm2.summary_id" in sql
        assert "FROM visible_mentions vm2" in sql
        assert "WHERE vm2.entity_id = vm.entity_id" in sql
        assert "ORDER BY vm2.mentioned_at ASC, vm2.summary_id ASC" in sql
        assert "LIMIT 1) AS first_in," in sql

    def test_includes_aggregate_columns(self) -> None:
        sql = entity_agg_cte(include_first_in=True)
        assert "COUNT(*) AS occ_count" in sql
        assert "MIN(vm.mentioned_at) AS first_at" in sql
        assert "MAX(vm.mentioned_at) AS last_at" in sql
        assert "json_group_array(DISTINCT vm.surface_form) AS visible_surfaces" in sql

    def test_starts_with_concat_safe_comma(self) -> None:
        # The CTE fragment begins with ", entity_agg AS (" so it can
        # be concatenated directly after VISIBLE_MENTIONS_CTE (which
        # ends with the close-paren of the WITH clause).
        assert entity_agg_cte(include_first_in=True).startswith(", entity_agg AS (")


class TestEntityAggCteNoFirstIn:
    """``entity_agg_cte(include_first_in=False)`` — used by ``lcm_search_entities``."""

    def test_byte_equals_vendored_fixture(self) -> None:
        expected = _read_fixture(_FIXTURE_AGG_FALSE)
        assert entity_agg_cte(include_first_in=False) == expected

    def test_length_locked(self) -> None:
        assert len(entity_agg_cte(include_first_in=False)) == 292

    def test_omits_first_in_subquery(self) -> None:
        # The variant skips the correlated subquery entirely —
        # search_entities doesn't need first_seen_in_summary_id.
        sql = entity_agg_cte(include_first_in=False)
        assert "first_in" not in sql
        assert "vm2" not in sql

    def test_preserves_other_aggregates(self) -> None:
        # Same shape minus first_in; all other columns must remain.
        sql = entity_agg_cte(include_first_in=False)
        assert "COUNT(*) AS occ_count" in sql
        assert "MIN(vm.mentioned_at) AS first_at" in sql
        assert "MAX(vm.mentioned_at) AS last_at" in sql
        assert "json_group_array(DISTINCT vm.surface_form) AS visible_surfaces" in sql

    def test_preserves_template_indent_blank_line(self) -> None:
        # JS template-literal substitution of an empty string into
        # `      ${firstInExpr}\n` produces a line of exactly 6 spaces
        # followed by a newline. The pre-commit `trailing-whitespace`
        # hook is configured to skip this fixture so the byte form is
        # stable in-tree. We assert the shape so a future regression
        # (e.g. someone strips it from the Python source) trips here.
        sql = entity_agg_cte(include_first_in=False)
        assert "\n      \n" in sql


class TestVariantDifference:
    """Cross-variant invariants — the two CTEs differ only in first_in."""

    def test_with_first_in_is_strictly_longer(self) -> None:
        assert len(entity_agg_cte(include_first_in=True)) > len(
            entity_agg_cte(include_first_in=False)
        )

    def test_difference_only_in_first_in_region(self) -> None:
        # If we strip the first_in subquery from the include variant,
        # what remains should be byte-identical to the no-first-in
        # variant modulo the 6-space placeholder. This guards against
        # accidental drift in the surrounding template.
        with_fi = entity_agg_cte(include_first_in=True)
        no_fi = entity_agg_cte(include_first_in=False)
        # Locate the first_in block in the with-fi variant — bounded
        # by the indented marker line and the closing "AS first_in,"
        # plus the following newline.
        start = with_fi.index("(SELECT vm2.summary_id")
        end = with_fi.index("AS first_in,") + len("AS first_in,")
        # Drop the block; the line that hosted it (6 spaces +
        # interpolation) collapses to the same 6 spaces + newline that
        # the no-first-in variant has by construction.
        stripped = with_fi[:start] + with_fi[end:]
        assert stripped == no_fi


# ---------------------------------------------------------------------------
# Compose with VISIBLE_MENTIONS_CTE — prepared-statement parse check
# ---------------------------------------------------------------------------


@pytest.fixture
def conn_with_v41_schema() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the v4.1 migration ladder applied.

    Provides the ``lcm_entities``, ``lcm_entity_mentions``, and
    ``summaries`` tables (plus their suppression columns) that the
    composed prepared statements need to resolve at parse time. We
    don't seed any rows — the test is about SQL validity, not data
    semantics.

    The migration is run with ``fts5_available=False`` because we
    don't exercise FTS surfaces here and skipping the FTS5 virtual
    tables sidesteps the "no fts5 module" failure on Python builds
    whose stdlib SQLite was compiled without FTS5.
    """
    conn = sqlite3.connect(":memory:")
    try:
        run_lcm_migrations(conn, fts5_available=False)
        yield conn
    finally:
        conn.close()


class TestComposeWithVisibleMentions:
    """Both variants must compose into a SQLite-parseable prepared statement."""

    def test_compose_with_first_in_parses(self, conn_with_v41_schema: sqlite3.Connection) -> None:
        sql = (
            f"{VISIBLE_MENTIONS_CTE}"
            f"{entity_agg_cte(include_first_in=True)}\n"
            "SELECT e.entity_id, e.session_key, e.canonical_text, e.entity_type,\n"
            "       ea.first_at AS first_seen_at,\n"
            "       ea.last_at  AS last_seen_at,\n"
            "       ea.first_in AS first_seen_in_summary_id,\n"
            "       ea.occ_count AS occurrence_count,\n"
            "       ea.visible_surfaces AS alternate_surfaces,\n"
            "       e.metadata\n"
            "  FROM lcm_entities e\n"
            "  JOIN entity_agg ea ON ea.entity_id = e.entity_id\n"
            " WHERE e.session_key = ? AND e.canonical_text = ? COLLATE NOCASE\n"
            " LIMIT 1"
        )
        # ``sqlite3.Connection.execute`` against an empty schema is
        # the cheapest way to validate that the SQL parses, binds
        # resolve, and the JOIN targets exist. An empty result set is
        # the expected outcome.
        with closing(conn_with_v41_schema.cursor()) as cur:
            cur.execute(sql, ("session-x", "Voyage"))
            assert cur.fetchall() == []

    def test_compose_no_first_in_parses(self, conn_with_v41_schema: sqlite3.Connection) -> None:
        # Mirrors what ``lcm_search_entities`` will emit (Epic 06):
        # browse-style query without the per-row first_in correlated
        # subquery. The EXISTS guard mirrors the porting-guide spec
        # at docs/porting-guides/tools.md:477.
        sql = (
            f"{VISIBLE_MENTIONS_CTE}"
            f"{entity_agg_cte(include_first_in=False)}\n"
            "SELECT e.entity_id, e.canonical_text, e.entity_type,\n"
            "       ea.first_at, ea.last_at, ea.occ_count,\n"
            "       ea.visible_surfaces\n"
            "  FROM lcm_entities e\n"
            "  JOIN entity_agg ea ON ea.entity_id = e.entity_id\n"
            " WHERE e.session_key = ?\n"
            "   AND EXISTS (\n"
            "         SELECT 1 FROM lcm_entity_mentions m\n"
            "           JOIN summaries s ON s.summary_id = m.summary_id\n"
            "          WHERE m.entity_id = e.entity_id AND s.suppressed_at IS NULL\n"
            "       )\n"
            " LIMIT 10"
        )
        with closing(conn_with_v41_schema.cursor()) as cur:
            cur.execute(sql, ("session-x",))
            assert cur.fetchall() == []

    def test_concat_is_well_formed(self) -> None:
        # The lexical concat (VISIBLE_MENTIONS_CTE + entity_agg_cte)
        # must produce a single WITH clause with two CTEs separated
        # by exactly one comma. We assert the substring positions
        # rather than re-running SQLite to keep the failure mode
        # crisp if the surrounding whitespace shifts.
        composed = f"{VISIBLE_MENTIONS_CTE}{entity_agg_cte(include_first_in=True)}"
        # Single WITH ... AS pair, comma-separated.
        assert composed.count("WITH visible_mentions AS") == 1
        assert ", entity_agg AS (" in composed
        # The CTE chain ends before any SELECT — callers append their
        # body, this helper never embeds one.
        assert "SELECT e." not in composed


# ---------------------------------------------------------------------------
# Module hygiene
# ---------------------------------------------------------------------------


class TestModuleSurface:
    """``entity_shared`` must be import-safe and export the right names."""

    def test_no_sql_execution_at_import(self) -> None:
        # Re-import the module under a fresh cache and verify it
        # doesn't accidentally open a sqlite3.Connection or otherwise
        # touch the DB layer at import time. We probe by checking
        # the module's globals for any sqlite3-flavored attributes.
        import lossless_hermes.tools.entity_shared as mod

        mod = importlib.reload(mod)
        attrs = vars(mod)
        # __all__ controls what `from x import *` exports; explicitly
        # check it equals the spec-mandated pair.
        assert attrs["__all__"] == ["VISIBLE_MENTIONS_CTE", "entity_agg_cte"]
        # No sqlite3 connections or cursors should be sitting in the
        # module namespace.
        for name, value in attrs.items():
            assert not isinstance(value, sqlite3.Connection), (
                f"{name} is a sqlite3.Connection — import-time side effect leaked"
            )
            assert not isinstance(value, sqlite3.Cursor), (
                f"{name} is a sqlite3.Cursor — import-time side effect leaked"
            )

    def test_exports_the_two_names_via_package(self) -> None:
        # The package-level re-export from `lossless_hermes.tools`
        # must expose both names. (Originally asserted "only these two"
        # via equality — relaxed in PR #72 merge once TypeBox helpers +
        # TOOL_SCHEMAS registry joined the package per ADR-016.)
        import lossless_hermes.tools as pkg

        assert "VISIBLE_MENTIONS_CTE" in pkg.__all__
        assert "entity_agg_cte" in pkg.__all__
        assert pkg.VISIBLE_MENTIONS_CTE is VISIBLE_MENTIONS_CTE
        assert pkg.entity_agg_cte is entity_agg_cte

    def test_entity_agg_cte_is_keyword_only(self) -> None:
        # The TS surface used an options object — Python equivalent is
        # a keyword-only argument. Calling positionally must fail so
        # the call-site reads `entity_agg_cte(include_first_in=...)`,
        # matching the TS ``{ includeFirstIn }`` destructure.
        with pytest.raises(TypeError):
            entity_agg_cte(True)  # type: ignore[misc]
