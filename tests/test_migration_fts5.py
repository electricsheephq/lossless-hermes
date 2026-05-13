"""Tests for :mod:`lossless_hermes.db.migration` FTS5 section helpers.

Covers the acceptance criteria from
``epics/01-storage/01-05-migration-fts5-tables.md``:

* All 3 virtual tables (``messages_fts``, ``summaries_fts``,
  ``summaries_fts_cjk``) are created on a fresh DB when FTS5 + trigram
  are available.
* When trigram is not available, only ``messages_fts`` + ``summaries_fts``
  are created — ``summaries_fts_cjk`` is gracefully skipped.
* When ``fts5_available=False``, the function is a clean no-op (no
  inspection, no drops, no creates).
* Stale-schema detection: a pre-existing FTS5 table with the legacy
  ``content_rowid='summary_id'`` config is dropped (along with its 5
  shadow tables) and recreated.
* Idempotency: re-running ``_ensure_fts5_tables`` against an already-
  migrated DB is a no-op.
* Functional smoke: ``UNINDEXED`` semantics, ``bm25()``, ``snippet()``,
  CJK trigram MATCH all work as expected per spike-005.
* Defensive regression guard: ``tokenize='not_a_real_tokenizer'`` raises
  ``OperationalError: no such tokenizer:`` — confirms our probe contract
  fails loudly when the SQLite build's tokenizer surface changes.

Out of scope (covered when the dependent PRs land):

* ConversationStore / SummaryStore write paths (#01-08, #01-09) — these
  do the per-row INSERT/DELETE; this test file only validates the
  initial bulk seed.
* FTS5 query sanitization helpers (#01-11).
* CJK detection / segmentation inside SummaryStore (#01-09).

References:

* :mod:`lossless_hermes.db.migration` — implementation under test.
* ``epics/01-storage/01-05-migration-fts5-tables.md`` — issue spec + AC.
* ``docs/porting-guides/storage.md`` §2.2 — FTS5 surface inventory.
* ``docs/spike-results/005-sqlite3-fts5-trigram.md`` — empirical FTS5 +
  trigram ground truth for stdlib ``sqlite3``.
* ``tests/fixtures/lcm_reference_schema.sql`` — TS-generated golden.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from lossless_hermes.db.features import DbFeatures, clear_db_features_cache
from lossless_hermes.db.migration import (
    _FTS_SPEC_MESSAGES_FTS,
    _FTS_SPEC_SUMMARIES_FTS,
    _FTS_SPEC_SUMMARIES_FTS_CJK,
    _ensure_fts5_tables,
    _ensure_standalone_fts_table,
    _get_existing_table_names,
    _get_fts_shadow_table_names,
    _quote_sql_identifier,
    _should_recreate_standalone_fts_table,
    run_lcm_migrations,
)

_REFERENCE_SCHEMA_PATH = Path(__file__).parent / "fixtures" / "lcm_reference_schema.sql"

# The 5 shadow tables FTS5 creates alongside any virtual table.
_SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_features_cache_around_each_test() -> Iterator[None]:
    """Module-level cache in ``db.features`` is shared across tests.

    Clear it before and after each test so per-test fixtures see a cold
    cache and so that no test leaks state into the next.
    """
    clear_db_features_cache()
    try:
        yield
    finally:
        clear_db_features_cache()


@pytest.fixture
def fresh_db() -> Iterator[sqlite3.Connection]:
    """An in-memory DB with ``PRAGMA foreign_keys = ON`` and no migrations applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def fts5_migrated_db(fresh_db: sqlite3.Connection) -> sqlite3.Connection:
    """A DB with the full migration ladder + FTS5 applied."""
    run_lcm_migrations(fresh_db, fts5_available=True)
    return fresh_db


def _list_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _get_table_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if row is None:
        return None
    return row[0]


# ---------------------------------------------------------------------------
# Identifier-quoting helper
# ---------------------------------------------------------------------------


def test_quote_sql_identifier_valid() -> None:
    assert _quote_sql_identifier("messages_fts") == '"messages_fts"'
    assert _quote_sql_identifier("_leading_underscore") == '"_leading_underscore"'
    assert _quote_sql_identifier("MixedCase42") == '"MixedCase42"'


def test_quote_sql_identifier_invalid() -> None:
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _quote_sql_identifier("contains space")
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _quote_sql_identifier("1leading_digit")
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _quote_sql_identifier("with;semicolon")
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _quote_sql_identifier("")


def test_quote_sql_identifier_rejects_embedded_quote() -> None:
    """An identifier containing ``"`` fails the regex bounds-check.

    The TS-source regex ``^[A-Za-z_][A-Za-z0-9_]*$`` rejects ANY non-word
    character, including embedded double-quotes. So even though the
    helper's quote-doubling escape logic is present (matching TS), the
    regex never lets such an identifier through. Both layers are
    belt-and-suspenders — this test pins the contract.
    """
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        _quote_sql_identifier('weird"name')


# ---------------------------------------------------------------------------
# Shadow-table name helper
# ---------------------------------------------------------------------------


def test_shadow_table_names_messages_fts() -> None:
    """``messages_fts`` shadow names match the 5-suffix contract."""
    assert _get_fts_shadow_table_names("messages_fts") == (
        "messages_fts_data",
        "messages_fts_idx",
        "messages_fts_content",
        "messages_fts_docsize",
        "messages_fts_config",
    )


def test_shadow_table_names_summaries_fts_cjk() -> None:
    """CJK FTS shadow names use the same suffix convention."""
    assert _get_fts_shadow_table_names("summaries_fts_cjk") == (
        "summaries_fts_cjk_data",
        "summaries_fts_cjk_idx",
        "summaries_fts_cjk_content",
        "summaries_fts_cjk_docsize",
        "summaries_fts_cjk_config",
    )


# ---------------------------------------------------------------------------
# _get_existing_table_names helper
# ---------------------------------------------------------------------------


def test_get_existing_table_names_returns_subset(fresh_db: sqlite3.Connection) -> None:
    fresh_db.execute("CREATE TABLE foo (id INTEGER)")
    fresh_db.execute("CREATE TABLE bar (id INTEGER)")
    result = _get_existing_table_names(fresh_db, ["foo", "bar", "baz"])
    assert result == {"foo", "bar"}


def test_get_existing_table_names_empty_candidates(
    fresh_db: sqlite3.Connection,
) -> None:
    """Empty iterable returns empty set (no SQL issued)."""
    assert _get_existing_table_names(fresh_db, []) == set()


# ---------------------------------------------------------------------------
# Positive path: full FTS5 + trigram
# ---------------------------------------------------------------------------


def test_creates_all_three_virtual_tables(
    fts5_migrated_db: sqlite3.Connection,
) -> None:
    """On a fresh DB with FTS5 + trigram available, all 3 virtual tables exist."""
    tables = _list_tables(fts5_migrated_db)
    assert "messages_fts" in tables
    assert "summaries_fts" in tables
    assert "summaries_fts_cjk" in tables


def test_creates_all_five_shadow_tables_per_virtual(
    fts5_migrated_db: sqlite3.Connection,
) -> None:
    """Each FTS5 virtual table comes with its 5 shadow tables."""
    tables = _list_tables(fts5_migrated_db)
    for base in ("messages_fts", "summaries_fts", "summaries_fts_cjk"):
        for suffix in _SHADOW_SUFFIXES:
            assert f"{base}{suffix}" in tables, f"shadow table {base}{suffix} missing"


def test_messages_fts_create_sql_matches_reference(
    fts5_migrated_db: sqlite3.Connection,
) -> None:
    """``sqlite_master.sql`` for messages_fts is byte-equivalent to the TS reference.

    Reference is at ``tests/fixtures/lcm_reference_schema.sql`` line 559:
    ``CREATE VIRTUAL TABLE messages_fts USING fts5(content,
    tokenize='porter unicode61')`` (modulo whitespace).
    """
    sql = _get_table_sql(fts5_migrated_db, "messages_fts")
    assert sql is not None
    # Normalize whitespace for comparison (matches schema_subset_check).
    import re

    normalized = re.sub(r"\s+", " ", sql).strip().lower()
    assert "create virtual table messages_fts using fts5(" in normalized
    assert "tokenize='porter unicode61'" in normalized


def test_summaries_fts_create_sql_includes_unindexed(
    fts5_migrated_db: sqlite3.Connection,
) -> None:
    """``summaries_fts`` declares ``summary_id UNINDEXED, content``."""
    sql = _get_table_sql(fts5_migrated_db, "summaries_fts")
    assert sql is not None
    import re

    normalized = re.sub(r"\s+", " ", sql).strip().lower()
    assert "summary_id unindexed" in normalized
    assert "tokenize='porter unicode61'" in normalized


def test_summaries_fts_cjk_uses_trigram_tokenizer(
    fts5_migrated_db: sqlite3.Connection,
) -> None:
    """``summaries_fts_cjk`` uses ``tokenize='trigram'``."""
    sql = _get_table_sql(fts5_migrated_db, "summaries_fts_cjk")
    assert sql is not None
    import re

    normalized = re.sub(r"\s+", " ", sql).strip().lower()
    assert "summary_id unindexed" in normalized
    assert "tokenize='trigram'" in normalized


# ---------------------------------------------------------------------------
# Seed behavior
# ---------------------------------------------------------------------------


def test_messages_fts_is_seeded_from_existing_messages(
    fresh_db: sqlite3.Connection,
) -> None:
    """Pre-existing messages are bulk-loaded into messages_fts on first run.

    Per LCM TS migration.ts:1204-1207, the seed runs immediately after
    CREATE VIRTUAL TABLE — so messages inserted BEFORE the FTS migration
    end up in the FTS index. The application layer (#01-08) is
    responsible for steady-state writes after that.
    """
    # First migrate WITHOUT FTS5, then insert messages, then migrate again
    # WITH FTS5 — the second migration's create step seeds the new FTS
    # table from existing messages.
    run_lcm_migrations(fresh_db, fts5_available=False)
    fresh_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = fresh_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    fresh_db.execute(
        "INSERT INTO messages (conversation_id, seq, role, content, token_count) "
        "VALUES (?, 1, 'user', 'hello world', 2)",
        (conv_id,),
    )
    fresh_db.commit()

    run_lcm_migrations(fresh_db, fts5_available=True)

    count = fresh_db.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    assert count == 1
    rows = fresh_db.execute(
        "SELECT rowid, content FROM messages_fts WHERE messages_fts MATCH 'hello'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "hello world"


def test_summaries_fts_is_seeded_with_summary_id_unindexed(
    fresh_db: sqlite3.Connection,
) -> None:
    """The UNINDEXED column round-trips through the seed step.

    Per spike-005 §"LCM's FTS5 surface used" — UNINDEXED columns are
    written to the FTS table but not tokenized. We verify the value is
    preserved on SELECT (i.e. SQLite stores it as a regular column).
    """
    run_lcm_migrations(fresh_db, fts5_available=False)
    fresh_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = fresh_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    fresh_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count) VALUES ('abc', ?, 'leaf', 'hello world', 2)",
        (conv_id,),
    )
    fresh_db.commit()

    run_lcm_migrations(fresh_db, fts5_available=True)

    rows = fresh_db.execute(
        "SELECT summary_id, content FROM summaries_fts WHERE summaries_fts MATCH 'hello'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "abc"
    assert rows[0][1] == "hello world"


def test_summaries_fts_cjk_is_seeded_for_cjk_match(
    fresh_db: sqlite3.Connection,
) -> None:
    """Trigram tokenizer enables CJK substring MATCH.

    Per spike-005 §"LCM's FTS5 surface used", the trigram tokenizer
    indexes every 3-character window — so MATCH queries shorter than 3
    chars return 0 rows. We test with a 3-character substring to verify
    the tokenizer is actually engaged and the table is properly seeded.
    """
    run_lcm_migrations(fresh_db, fts5_available=False)
    fresh_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = fresh_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    fresh_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count) VALUES ('a', ?, 'leaf', '你好世界', 4)",
        (conv_id,),
    )
    fresh_db.commit()

    run_lcm_migrations(fresh_db, fts5_available=True)

    # 3-character substring "好世界" appears within the 4-character content.
    rows = fresh_db.execute(
        "SELECT summary_id, content FROM summaries_fts_cjk WHERE summaries_fts_cjk MATCH '好世界'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "a"
    assert rows[0][1] == "你好世界"


# ---------------------------------------------------------------------------
# Negative paths: fts5_available=False / trigram=False
# ---------------------------------------------------------------------------


def test_fts5_unavailable_is_noop(fresh_db: sqlite3.Connection) -> None:
    """``fts5_available=False`` skips all FTS5 work entirely.

    The function does not inspect the existing schema, does not attempt
    drops, does not call the trigram probe, and does not create any FTS
    tables.
    """
    run_lcm_migrations(fresh_db, fts5_available=False)

    tables = _list_tables(fresh_db)
    assert "messages_fts" not in tables
    assert "summaries_fts" not in tables
    assert "summaries_fts_cjk" not in tables


def test_fts5_unavailable_does_not_probe_features(
    fresh_db: sqlite3.Connection,
) -> None:
    """``fts5_available=False`` must NOT call :func:`get_lcm_db_features`.

    The TS source's early-return on ``fts5Available=false`` skips the
    feature probe entirely. Verifies the Python port honors the same
    contract — important when the caller knows FTS5 is unavailable
    (e.g. on a custom-compiled Python without ``--enable-fts5``) and
    doesn't want the no-op probe overhead.
    """
    with patch("lossless_hermes.db.migration.get_lcm_db_features") as mock_probe:
        _ensure_fts5_tables(fresh_db, fts5_available=False)
        mock_probe.assert_not_called()


def test_trigram_unavailable_skips_cjk_table(
    fresh_db: sqlite3.Connection,
) -> None:
    """When trigram probe returns False, ``summaries_fts_cjk`` is skipped.

    Patches :func:`get_lcm_db_features` to return ``fts5_trigram_available=False``
    so we exercise the skip path without depending on the build's actual
    trigram availability.
    """
    fake = DbFeatures(fts5_available=True, fts5_trigram_available=False, vec0_available=False)
    with patch("lossless_hermes.db.migration.get_lcm_db_features", return_value=fake):
        run_lcm_migrations(fresh_db, fts5_available=True)

    tables = _list_tables(fresh_db)
    assert "messages_fts" in tables
    assert "summaries_fts" in tables
    assert "summaries_fts_cjk" not in tables


def test_trigram_unavailable_drops_stale_cjk(fresh_db: sqlite3.Connection) -> None:
    """When trigram is False but a stale CJK table exists, it's dropped.

    Simulates a DB that was migrated when trigram WAS available and is
    now being re-migrated on a stripped-down build. Per TS contract at
    migration.ts:1186-1192 the migration must clean up the stale virtual
    table best-effort.
    """
    # First migrate with trigram available, then re-run with it patched to False.
    run_lcm_migrations(fresh_db, fts5_available=True)
    assert "summaries_fts_cjk" in _list_tables(fresh_db)
    clear_db_features_cache()

    fake = DbFeatures(fts5_available=True, fts5_trigram_available=False, vec0_available=False)
    with patch("lossless_hermes.db.migration.get_lcm_db_features", return_value=fake):
        run_lcm_migrations(fresh_db, fts5_available=True)

    assert "summaries_fts_cjk" not in _list_tables(fresh_db)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotency_fts5_second_run_no_op(fresh_db: sqlite3.Connection) -> None:
    """Re-running the migration with FTS5 enabled does not recreate the tables.

    Captures a SHA-style fingerprint of the FTS5 schema after the first
    run; the fingerprint must be identical after the second run.
    """
    run_lcm_migrations(fresh_db, fts5_available=True)
    snapshot_1 = fresh_db.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE name LIKE '%_fts%' OR name LIKE 'messages_fts%' "
        "ORDER BY name"
    ).fetchall()
    clear_db_features_cache()

    run_lcm_migrations(fresh_db, fts5_available=True)
    snapshot_2 = fresh_db.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE name LIKE '%_fts%' OR name LIKE 'messages_fts%' "
        "ORDER BY name"
    ).fetchall()

    assert snapshot_1 == snapshot_2


def test_idempotency_direct_ensure_call(
    fts5_migrated_db: sqlite3.Connection,
) -> None:
    """Calling ``_ensure_fts5_tables`` again directly is a no-op."""
    schema_before = fts5_migrated_db.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()

    _ensure_fts5_tables(fts5_migrated_db, fts5_available=True)

    schema_after = fts5_migrated_db.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    assert schema_before == schema_after


# ---------------------------------------------------------------------------
# Stale-schema recreate
# ---------------------------------------------------------------------------


def test_stale_schema_pattern_triggers_recreate(
    fresh_db: sqlite3.Connection,
) -> None:
    """A pre-existing summaries_fts with ``content_rowid='summary_id'`` is recreated.

    Per migration.ts:1228-1231, the spec's ``stale_schema_patterns`` are
    substring-matched against the existing table's ``sqlite_master.sql``.
    On match, the table + its 5 shadows are dropped and the table is
    recreated with the current shape.
    """
    # Manually create the legacy shape this PR explicitly replaces.
    fresh_db.execute(
        "CREATE VIRTUAL TABLE summaries_fts USING fts5("
        "summary_id, content, content='summaries', content_rowid='summary_id', "
        "tokenize='porter unicode61')"
    )
    legacy_sql = _get_table_sql(fresh_db, "summaries_fts")
    assert legacy_sql is not None
    assert "content_rowid" in legacy_sql

    # Now run the migration — the stale-schema detector should trigger
    # a DROP + recreate.
    run_lcm_migrations(fresh_db, fts5_available=True)

    new_sql = _get_table_sql(fresh_db, "summaries_fts")
    assert new_sql is not None
    assert "content_rowid" not in new_sql
    assert "summary_id UNINDEXED" in new_sql


def test_should_recreate_missing_table(fresh_db: sqlite3.Connection) -> None:
    """An absent FTS table is flagged for recreate."""
    assert _should_recreate_standalone_fts_table(fresh_db, _FTS_SPEC_MESSAGES_FTS) is True


def test_should_recreate_missing_shadow_table(fresh_db: sqlite3.Connection) -> None:
    """A virtual table whose shadow tables are partially missing is flagged.

    Simulates a half-created state by creating the virtual table then
    manually dropping one shadow table. The detector returns True so the
    caller will purge and recreate.
    """
    fresh_db.execute(
        "CREATE VIRTUAL TABLE messages_fts USING fts5(content, tokenize='porter unicode61')"
    )
    # Drop one of the shadow tables to simulate corruption.
    fresh_db.execute("DROP TABLE messages_fts_idx")

    assert _should_recreate_standalone_fts_table(fresh_db, _FTS_SPEC_MESSAGES_FTS) is True


def test_should_recreate_missing_expected_column(
    fresh_db: sqlite3.Connection,
) -> None:
    """An FTS table missing one of ``spec.expected_columns`` is flagged.

    We can't easily create an FTS5 virtual table missing a column the
    spec requires (FTS5 requires at least one column). So we mock the
    PRAGMA table_info return by using a temporary spec with different
    expected_columns than what was created.
    """
    fresh_db.execute(
        "CREATE VIRTUAL TABLE messages_fts USING fts5(content, tokenize='porter unicode61')"
    )
    # Use the existing virtual table but pretend the spec expects an
    # ``extra`` column that's not declared on the table.
    from lossless_hermes.db.migration import _FtsTableSpec

    spec = _FtsTableSpec(
        table_name="messages_fts",
        create_sql=_FTS_SPEC_MESSAGES_FTS.create_sql,
        seed_sql=_FTS_SPEC_MESSAGES_FTS.seed_sql,
        expected_columns=("content", "extra"),
    )
    assert _should_recreate_standalone_fts_table(fresh_db, spec) is True


def test_should_not_recreate_intact_table(
    fts5_migrated_db: sqlite3.Connection,
) -> None:
    """An intact, current-shape FTS table is NOT flagged for recreate."""
    assert _should_recreate_standalone_fts_table(fts5_migrated_db, _FTS_SPEC_MESSAGES_FTS) is False
    assert _should_recreate_standalone_fts_table(fts5_migrated_db, _FTS_SPEC_SUMMARIES_FTS) is False
    assert (
        _should_recreate_standalone_fts_table(fts5_migrated_db, _FTS_SPEC_SUMMARIES_FTS_CJK)
        is False
    )


# ---------------------------------------------------------------------------
# Functional smoke tests — spike-005 invariants
# ---------------------------------------------------------------------------


def test_bm25_returns_descending_relevance(
    fresh_db: sqlite3.Connection,
) -> None:
    """``bm25()`` orders rows by relevance (most-relevant first).

    Per spike-005 §"LCM's FTS5 surface used" — bm25 ranking is required
    by SummaryStore (#01-09). Negative bm25 values + ASC ordering = most
    relevant first.
    """
    run_lcm_migrations(fresh_db, fts5_available=True)
    fresh_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = fresh_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Insert summaries with varying term frequencies of "important".
    for sid, content in (
        ("a", "important important important detail"),
        ("b", "important detail"),
        ("c", "completely unrelated content"),
    ):
        fresh_db.execute(
            "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
            "token_count) VALUES (?, ?, 'leaf', ?, 1)",
            (sid, conv_id, content),
        )
    fresh_db.commit()
    # Re-seed — alternatively the FTS table is already empty since the
    # migration ran before the INSERTs. So manually populate.
    fresh_db.execute(
        "INSERT INTO summaries_fts(summary_id, content) SELECT summary_id, content FROM summaries"
    )

    rows = fresh_db.execute(
        "SELECT summary_id FROM summaries_fts "
        "WHERE summaries_fts MATCH 'important' "
        "ORDER BY bm25(summaries_fts) ASC"
    ).fetchall()
    # Most relevant (3 hits) should come first.
    assert rows[0][0] == "a"


def test_snippet_returns_substring_around_match(
    fresh_db: sqlite3.Connection,
) -> None:
    """``snippet(<table>, col, '', '', '...', 10)`` returns ~10 tokens around the match."""
    run_lcm_migrations(fresh_db, fts5_available=True)
    fresh_db.execute("INSERT INTO conversations (session_id) VALUES ('s1')")
    conv_id = fresh_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    fresh_db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count) VALUES ('a', ?, 'leaf', "
        "'lots of preamble text before the keyword landing in the middle "
        "and some trailing content after', 1)",
        (conv_id,),
    )
    fresh_db.commit()
    fresh_db.execute(
        "INSERT INTO summaries_fts(summary_id, content) SELECT summary_id, content FROM summaries"
    )

    row = fresh_db.execute(
        "SELECT snippet(summaries_fts, 1, '', '', '...', 10) "
        "FROM summaries_fts WHERE summaries_fts MATCH 'keyword'"
    ).fetchone()
    assert row is not None
    assert "keyword" in row[0]


# ---------------------------------------------------------------------------
# Defensive: non-existent tokenizer raises loudly
# ---------------------------------------------------------------------------


def test_invalid_tokenizer_raises_operational_error(
    fresh_db: sqlite3.Connection,
) -> None:
    """``tokenize='not_a_real_tokenizer'`` raises ``OperationalError``.

    Per spike-005 §"Sanity test" — a defensive regression guard. If the
    SQLite build's tokenizer surface ever changes (e.g. a future build
    silently aliases unknown tokenizers to unicode61), this test fires
    loudly so we update the FTS feature probe and migration to match.
    """
    with pytest.raises(sqlite3.OperationalError, match="no such tokenizer"):
        fresh_db.execute(
            "CREATE VIRTUAL TABLE bogus USING fts5(content, tokenize='not_a_real_tokenizer')"
        )


# ---------------------------------------------------------------------------
# Reference-fixture parity for FTS5 objects
# ---------------------------------------------------------------------------


def test_fts5_objects_present_in_reference_fixture() -> None:
    """The reference fixture contains every FTS5 object this PR creates.

    Sanity check that the committed golden schema covers the 3 FTS5
    virtual tables. If this fails, the reference fixture is stale (the
    LCM source has drifted from commit ``1f07fbd``).
    """
    if not _REFERENCE_SCHEMA_PATH.exists():
        pytest.skip("reference fixture not present")

    text = _REFERENCE_SCHEMA_PATH.read_text(encoding="utf-8")
    for table in ("messages_fts", "summaries_fts", "summaries_fts_cjk"):
        assert f"-- table: {table}\n" in text, (
            f"FTS5 virtual table {table!r} missing from reference fixture"
        )


def test_direct_ensure_standalone_fts_table(fresh_db: sqlite3.Connection) -> None:
    """The standalone-ensure helper can be called directly with a custom spec.

    Verifies the public-ish helper API (used by ``_ensure_fts5_tables``)
    so future callers (e.g. a /lcm doctor rebuild path) can rely on it.
    Requires the parent ``messages`` table to exist so the seed step
    doesn't fail.
    """
    fresh_db.execute("CREATE TABLE messages (message_id INTEGER PRIMARY KEY, content TEXT)")
    fresh_db.execute("INSERT INTO messages (message_id, content) VALUES (1, 'hi')")

    _ensure_standalone_fts_table(fresh_db, _FTS_SPEC_MESSAGES_FTS)

    assert "messages_fts" in _list_tables(fresh_db)
    rows = fresh_db.execute("SELECT rowid, content FROM messages_fts").fetchall()
    assert rows == [(1, "hi")]
