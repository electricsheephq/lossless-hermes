"""Smoke test for the test harness itself (issue 00-04).

Proves three things:

1. The asymmetric matchers in ``tests/_matchers.py`` compare correctly
   via ``__eq__`` (the AC pair from issue 00-04: ``AnyOf(int) == 42`` and
   ``ContainsString("hello") == "hello world"``).
2. The implemented fixtures from ``conftest.py`` (``tmp_home``,
   ``db_in_memory``, ``test_corpus``) wire up and yield real values.
3. The still-placeholder fixtures (``db_with_vec0``, ``fake_voyage``,
   ``fake_llm``) are discoverable and raise ``NotImplementedError`` with
   a pointer to the epic that will port them — i.e., the shape is locked
   even though the body waits.

``test_corpus`` was filled by issue 09-08 (the Voyage-recall benchmark
named the ``v41-test-corpus`` port as its own prerequisite); its smoke
assertion below is now positive rather than a ``NotImplementedError``
check. The remaining placeholder assertions should likewise flip to
real fixture usage as the relevant epics land.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from tests._matchers import (
    AnyOf,
    ContainsArray,
    ContainsObject,
    ContainsString,
    MatchesString,
)


# ---------------------------------------------------------------------------
# Matchers — direct AC from issue 00-04
# ---------------------------------------------------------------------------


def test_any_of_int_matches_42() -> None:
    """AC item: ``AnyOf(int) == 42`` evaluates True."""
    assert AnyOf(int) == 42


def test_contains_string_matches_hello_world() -> None:
    """AC item: ``ContainsString("hello") == "hello world"`` evaluates True."""
    assert ContainsString("hello") == "hello world"


# ---------------------------------------------------------------------------
# Matchers — round-trip the full set so the shapes are covered before any
# downstream test relies on them. Each pair exercises a True case and a
# False case so __eq__ is genuinely tested (not just truthiness).
# ---------------------------------------------------------------------------


def test_any_of_rejects_wrong_type() -> None:
    assert AnyOf(int) != "42"


def test_contains_object_matches_subset() -> None:
    assert ContainsObject({"role": "user"}) == {"role": "user", "msg": "hi"}


def test_contains_object_rejects_missing_key() -> None:
    assert ContainsObject({"role": "user"}) != {"msg": "hi"}


def test_contains_object_rejects_non_dict() -> None:
    assert ContainsObject({"a": 1}) != [("a", 1)]


def test_contains_string_rejects_missing_substring() -> None:
    assert ContainsString("hello") != "goodbye world"


def test_contains_string_rejects_non_string() -> None:
    assert ContainsString("hello") != 42


def test_contains_array_matches_subset() -> None:
    assert ContainsArray([1, 2]) == [1, 2, 3]


def test_contains_array_rejects_missing_item() -> None:
    assert ContainsArray([1, 4]) != [1, 2, 3]


def test_matches_string_finds_pattern() -> None:
    assert MatchesString(r"hello.*world") == "say hello to the world"


def test_matches_string_rejects_no_match() -> None:
    assert MatchesString(r"^hello$") != "hello world"


def test_repr_is_readable_for_failure_messages() -> None:
    """The ``__repr__`` of each matcher must be readable so pytest failure
    messages stay actionable per ADR-028 §Decision 6."""
    assert repr(AnyOf(int)) == "AnyOf(int)"
    assert repr(ContainsString("x")) == "ContainsString('x')"
    assert "role" in repr(ContainsObject({"role": "user"}))
    assert "1" in repr(ContainsArray([1, 2]))
    assert "hello" in repr(MatchesString(r"hello"))


# ---------------------------------------------------------------------------
# Implemented fixtures
# ---------------------------------------------------------------------------


def test_tmp_home_creates_hermes_state_dir(tmp_home: Path) -> None:
    """``tmp_home`` yields a tmpdir and sets ``HERMES_HOME`` to a
    pre-created ``.hermes/`` inside it."""

    assert tmp_home.exists()
    assert tmp_home.is_dir()

    hermes_home = os.environ.get("HERMES_HOME")
    assert hermes_home is not None
    assert Path(hermes_home).exists()
    assert Path(hermes_home).is_dir()
    assert Path(hermes_home).parent == tmp_home
    assert os.environ.get("HOME") == str(tmp_home)


def test_db_in_memory_yields_open_connection(db_in_memory: sqlite3.Connection) -> None:
    """``db_in_memory`` yields an open ``:memory:`` SQLite connection.

    No migrations are run yet (Epic 01) — but the connection must accept a
    trivial DDL/DML round-trip so the seam is proven."""

    cur = db_in_memory.execute("CREATE TABLE t (x INTEGER PRIMARY KEY)")
    db_in_memory.execute("INSERT INTO t (x) VALUES (1), (2), (3)")
    cur = db_in_memory.execute("SELECT count(*) FROM t")
    assert cur.fetchone()[0] == 3


# ---------------------------------------------------------------------------
# Placeholder fixtures — shape locked, body pending
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    ["db_with_vec0", "fake_voyage", "fake_llm"],
)
def test_placeholder_fixture_raises_not_implemented(
    request: pytest.FixtureRequest, fixture_name: str
) -> None:
    """Each not-yet-ported fixture is discoverable and raises
    ``NotImplementedError`` with the epic that will port it.

    This is what 'shape locked, body pending' means in practice:
    the fixture is registered with conftest, can be `getfixturevalue`'d,
    and fails with a useful pointer rather than ``fixture 'X' not found``.

    Note: ``test_corpus`` was on this list until issue 09-08 ported the
    ``v41-test-corpus`` fixture and filled the conftest body. It is now
    covered by :func:`test_corpus_fixture_seeds_real_rows` below.
    """

    with pytest.raises(NotImplementedError, match=r"Epic"):
        request.getfixturevalue(fixture_name)


def test_corpus_fixture_seeds_real_rows(test_corpus: dict[str, object]) -> None:
    """``test_corpus`` is now a real fixture (filled by issue 09-08).

    It ports ``v41-test-corpus.ts`` and seeds the synthetic corpus into
    the yielded ``db_in_memory`` connection. The smoke check here is
    minimal — the fixture's own exhaustive coverage lives in
    ``tests/fixtures/test_test_corpus.py``.
    """

    assert test_corpus["leaf_count"] == 54
    assert test_corpus["condensed_count"] == 2
    assert test_corpus["entity_count"] == 4
