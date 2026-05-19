"""Tests for :mod:`lossless_hermes.eval.query_set` — query set CRUD.

Ports ``lossless-claw/test/eval-query-set.test.ts`` (commit ``1f07fbd``
on branch ``pr-613``).

Covers:

* Encoding round-trip — :func:`encode_query_set_id` /
  :func:`decode_query_set_id`.
* Registration idempotency — same content is a no-op, different content
  raises.
* Stratum validation — invalid stratum rejected at register time.
* Empty-set rejection at register time.
* Round-trip ``expected_summary_ids`` JSON column.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySetIdentity,
    decode_query_set_id,
    encode_query_set_id,
    get_query_set,
    list_query_sets,
    register_query_set,
)


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Encoding round-trip
# ---------------------------------------------------------------------------


class TestEncoding:
    def test_encode_basic(self) -> None:
        assert encode_query_set_id(QuerySetIdentity(name="foo", version=1)) == "foo@v1"

    def test_encode_rejects_empty_name(self) -> None:
        with pytest.raises(ValueError, match="must be non-empty"):
            encode_query_set_id(QuerySetIdentity(name="", version=1))

    def test_encode_rejects_zero_version(self) -> None:
        with pytest.raises(ValueError, match="positive integer"):
            encode_query_set_id(QuerySetIdentity(name="foo", version=0))

    def test_encode_rejects_negative_version(self) -> None:
        with pytest.raises(ValueError, match="positive integer"):
            encode_query_set_id(QuerySetIdentity(name="foo", version=-1))

    def test_decode_round_trips(self) -> None:
        identity = QuerySetIdentity(name="my-set", version=3)
        assert decode_query_set_id(encode_query_set_id(identity)) == identity

    def test_decode_handles_at_v_in_name(self) -> None:
        """Names containing ``@v`` round-trip using rfind on the separator."""
        identity = QuerySetIdentity(name="foo@v0-legacy", version=2)
        encoded = encode_query_set_id(identity)
        assert encoded == "foo@v0-legacy@v2"
        assert decode_query_set_id(encoded) == identity

    def test_decode_rejects_missing_separator(self) -> None:
        with pytest.raises(ValueError, match="missing"):
            decode_query_set_id("no-separator")

    def test_decode_rejects_non_numeric_version(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            decode_query_set_id("foo@vXYZ")


# ---------------------------------------------------------------------------
# Registration + lookup
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_and_read_back(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set-1", version=1)
        queries = (
            QueryRecord(
                query_id="q1",
                query_text="hello",
                stratum="fts-easy",
                expected_summary_ids=("sum_a", "sum_b"),
            ),
            QueryRecord(
                query_id="q2",
                query_text="world",
                stratum="paraphrastic",
            ),
        )
        register_query_set(db, identity, queries)
        loaded = get_query_set(db, identity)
        assert loaded is not None
        assert loaded.identity == identity
        loaded_q1 = next(q for q in loaded.queries if q.query_id == "q1")
        assert loaded_q1.query_text == "hello"
        assert loaded_q1.stratum == "fts-easy"
        assert loaded_q1.expected_summary_ids == ("sum_a", "sum_b")
        loaded_q2 = next(q for q in loaded.queries if q.query_id == "q2")
        assert loaded_q2.expected_summary_ids is None

    def test_register_idempotent_same_content(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set-1", version=1)
        queries = (QueryRecord(query_id="q1", query_text="hello", stratum="fts-easy"),)
        register_query_set(db, identity, queries)
        # Second call with same content is a no-op (no exception).
        register_query_set(db, identity, queries)
        loaded = get_query_set(db, identity)
        assert loaded is not None
        assert len(loaded.queries) == 1

    def test_register_rejects_different_content_same_identity(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="set-1", version=1)
        register_query_set(
            db,
            identity,
            (QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),),
        )
        with pytest.raises(ValueError, match="different content"):
            register_query_set(
                db,
                identity,
                (QueryRecord(query_id="q1", query_text="DIFFERENT", stratum="fts-easy"),),
            )

    def test_register_rejects_empty_set(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="empty query set"):
            register_query_set(db, QuerySetIdentity(name="set-1", version=1), ())

    def test_register_rejects_duplicate_query_id(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="duplicate query_id"):
            register_query_set(
                db,
                QuerySetIdentity(name="set-1", version=1),
                (
                    QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),
                    QueryRecord(query_id="q1", query_text="B", stratum="fts-easy"),
                ),
            )

    def test_register_rejects_invalid_stratum(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid stratum"):
            register_query_set(
                db,
                QuerySetIdentity(name="set-1", version=1),
                (
                    QueryRecord(
                        query_id="q1",
                        query_text="A",
                        stratum="bogus",  # type: ignore[arg-type]
                    ),
                ),
            )

    def test_register_rejects_empty_query_text(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="empty query_text"):
            register_query_set(
                db,
                QuerySetIdentity(name="set-1", version=1),
                (QueryRecord(query_id="q1", query_text="", stratum="fts-easy"),),
            )

    def test_register_rejects_empty_query_id(self, db: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="missing query_id"):
            register_query_set(
                db,
                QuerySetIdentity(name="set-1", version=1),
                (QueryRecord(query_id="", query_text="A", stratum="fts-easy"),),
            )

    def test_get_query_set_returns_none_when_absent(self, db: sqlite3.Connection) -> None:
        assert get_query_set(db, QuerySetIdentity(name="no-such", version=1)) is None

    def test_list_query_sets_sorted(self, db: sqlite3.Connection) -> None:
        register_query_set(
            db,
            QuerySetIdentity(name="b", version=1),
            (QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),),
        )
        register_query_set(
            db,
            QuerySetIdentity(name="a", version=2),
            (QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),),
        )
        register_query_set(
            db,
            QuerySetIdentity(name="a", version=1),
            (QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),),
        )
        identities = list_query_sets(db)
        # Sorted by encoded id ASC; "a@v1" < "a@v2" < "b@v1".
        assert identities == [
            QuerySetIdentity(name="a", version=1),
            QuerySetIdentity(name="a", version=2),
            QuerySetIdentity(name="b", version=1),
        ]
