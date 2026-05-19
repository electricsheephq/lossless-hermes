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
* Transaction rollback — a half-written set never survives a failure.
* Row-ID namespacing — ``${querySetId}::${queryId}`` on write, stripped
  on read.
* Version isolation — ``name@v1`` and ``name@v2`` are independent.
* Order-independence — content signature ignores query list order.
* Corrupt ``expected_sources`` JSON is tolerated (read as ``None``).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.eval.query_set import (
    ROW_ID_SEPARATOR,
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

    def test_encode_rejects_non_integer_version(self) -> None:
        """TS ``eval-query-set.test.ts:65`` — ``version: 1.5`` is rejected.

        ``isinstance(1.5, int)`` is ``False`` in Python, so the
        positive-integer guard catches it (a ``float`` is never a valid
        version even when it equals a whole number).
        """
        with pytest.raises(ValueError, match="positive integer"):
            encode_query_set_id(QuerySetIdentity(name="x", version=1.5))  # type: ignore[arg-type]

    def test_encode_round_trips_v2_and_v7(self) -> None:
        """TS ``eval-query-set.test.ts:43-50`` — encode is the inverse of decode."""
        assert encode_query_set_id(QuerySetIdentity(name="eva-baseline", version=2)) == (
            "eva-baseline@v2"
        )
        encoded7 = encode_query_set_id(QuerySetIdentity(name="eva-baseline", version=7))
        assert decode_query_set_id(encoded7) == QuerySetIdentity(name="eva-baseline", version=7)

    def test_decode_round_trips(self) -> None:
        identity = QuerySetIdentity(name="my-set", version=3)
        assert decode_query_set_id(encode_query_set_id(identity)) == identity

    def test_decode_handles_at_v_in_name(self) -> None:
        """Names containing ``@v`` round-trip using rfind on the separator."""
        identity = QuerySetIdentity(name="foo@v0-legacy", version=2)
        encoded = encode_query_set_id(identity)
        assert encoded == "foo@v0-legacy@v2"
        assert decode_query_set_id(encoded) == identity

    def test_encode_at_v_in_name_separator_collision_round_trip(self) -> None:
        """TS ``eval-query-set.test.ts:52-56`` — ``weird@vname`` round-trips.

        The encoder appends ``@v1``; the decoder uses ``rfind`` so it
        splits on the LAST ``@v`` and reconstructs the embedded one.
        """
        identity = QuerySetIdentity(name="weird@vname", version=1)
        encoded = encode_query_set_id(identity)
        assert encoded == "weird@vname@v1"
        assert decode_query_set_id(encoded) == identity

    def test_decode_rejects_missing_separator(self) -> None:
        with pytest.raises(ValueError, match="missing"):
            decode_query_set_id("no-separator")

    def test_decode_rejects_non_numeric_version(self) -> None:
        with pytest.raises(ValueError, match="malformed"):
            decode_query_set_id("foo@vXYZ")

    def test_decode_rejects_empty_name(self) -> None:
        """TS ``eval-query-set.test.ts:71`` — ``"@v1"`` has an empty name.

        The separator is present and the version parses, but the name
        before it is empty — that's malformed.
        """
        with pytest.raises(ValueError, match="malformed"):
            decode_query_set_id("@v1")


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

    def test_register_idempotent_with_shuffled_copy(self, db: sqlite3.Connection) -> None:
        """TS ``eval-query-set.test.ts:109-118`` — re-register with a
        reordered copy is a no-op.

        The content signature sorts queries by ``query_id`` before
        hashing, so ``[q3, q1, q2]`` and ``[q1, q2, q3]`` hash equal.
        """
        identity = QuerySetIdentity(name="eva-baseline", version=1)
        original = (
            QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),
            QueryRecord(query_id="q2", query_text="B", stratum="fts-medium"),
            QueryRecord(query_id="q3", query_text="C", stratum="paraphrastic"),
        )
        register_query_set(db, identity, original)
        # Re-register with a shuffled copy — same content, different order.
        shuffled = (original[2], original[0], original[1])
        register_query_set(db, identity, shuffled)  # must NOT raise
        loaded = get_query_set(db, identity)
        assert loaded is not None
        assert len(loaded.queries) == 3

    def test_versions_are_isolated(self, db: sqlite3.Connection) -> None:
        """TS ``eval-query-set.test.ts:131-144`` — ``name@v1`` and
        ``name@v2`` are independent rows.

        Registering a v2 with entirely different queries leaves v1's
        content untouched.
        """
        v1_queries = (
            QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),
            QueryRecord(query_id="q2", query_text="B", stratum="fts-medium"),
            QueryRecord(query_id="q3", query_text="C", stratum="paraphrastic"),
        )
        register_query_set(db, QuerySetIdentity(name="x", version=1), v1_queries)
        v2_queries = (QueryRecord(query_id="qNEW", query_text="v2 query", stratum="fts-easy"),)
        register_query_set(db, QuerySetIdentity(name="x", version=2), v2_queries)

        v1 = get_query_set(db, QuerySetIdentity(name="x", version=1))
        v2 = get_query_set(db, QuerySetIdentity(name="x", version=2))
        assert v1 is not None and v2 is not None
        assert len(v1.queries) == 3
        assert len(v2.queries) == 1
        assert v2.queries[0].query_id == "qNEW"

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

    def test_list_query_sets_empty_for_fresh_db(self, db: sqlite3.Connection) -> None:
        """TS ``eval-query-set.test.ts:193-196`` — fresh DB lists nothing."""
        assert list_query_sets(db) == []


# ---------------------------------------------------------------------------
# Transaction rollback — a half-written set never survives a failure
# ---------------------------------------------------------------------------


class TestTransactionRollback:
    """TS ``eval-query-set.test.ts:174-189`` — "rolls back on failure".

    The acceptance criterion (09-01) is explicit: "partial writes never
    survive a crash mid-loop (test by injecting an ``IntegrityError`` on
    the Nth row and asserting empty table)."
    """

    def test_partial_write_rolled_back_on_nth_row_failure(self, db: sqlite3.Connection) -> None:
        """Inject a PK collision on the 2nd query row → whole INSERT rolls back.

        ``register_query_set`` namespaces a row's PK as
        ``${querySetId}::${queryId}``. We pre-seed a ``lcm_eval_query``
        row whose PK collides with what the 2nd query *would* get. There
        is NO ``lcm_eval_query_set`` header for ``set@v1`` yet, so
        ``get_query_set`` returns ``None`` and registration proceeds
        into the INSERT loop — where the 2nd row hits an
        ``IntegrityError`` (UNIQUE PK violation). The transaction must
        roll back: no header row, and the colliding pre-seeded row is
        the ONLY ``lcm_eval_query`` row left.
        """
        identity = QuerySetIdentity(name="set", version=1)
        query_set_id = encode_query_set_id(identity)
        colliding_pk = f"{query_set_id}{ROW_ID_SEPARATOR}q2"

        # Pre-seed a row that collides with q2's namespaced PK. It needs
        # its own (different) parent query_set so the FK is satisfied.
        db.execute(
            "INSERT INTO lcm_eval_query_set (query_set_id, version) VALUES (?, ?)",
            ("decoy@v1", 1),
        )
        db.execute(
            """
            INSERT INTO lcm_eval_query
              (query_id, query_set_id, query_text, stratum,
               expected_topics, rubric)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (colliding_pk, "decoy@v1", "decoy", "fts-easy", "[]", "{}"),
        )

        queries = (
            QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),
            QueryRecord(query_id="q2", query_text="B", stratum="fts-medium"),
            QueryRecord(query_id="q3", query_text="C", stratum="paraphrastic"),
        )
        # The INSERT loop hits the PK collision on q2 → IntegrityError.
        with pytest.raises(sqlite3.IntegrityError):
            register_query_set(db, identity, queries)

        # Rollback proof: no header row for set@v1 was committed.
        header = db.execute(
            "SELECT COUNT(*) FROM lcm_eval_query_set WHERE query_set_id = ?",
            (query_set_id,),
        ).fetchone()[0]
        assert header == 0
        # And no set@v1 query rows survive — only the decoy pre-seed.
        own_rows = db.execute(
            "SELECT COUNT(*) FROM lcm_eval_query WHERE query_set_id = ?",
            (query_set_id,),
        ).fetchone()[0]
        assert own_rows == 0
        # get_query_set agrees the set does not exist.
        assert get_query_set(db, identity) is None

    def test_existing_set_unchanged_when_reregister_with_diff_content_fails(
        self, db: sqlite3.Connection
    ) -> None:
        """TS ``eval-query-set.test.ts:174-189`` — original survives a
        failed mutating re-register.

        Registering ``set@v1`` then attempting a different-content
        re-register raises *before* any write — so v1 keeps its
        ORIGINAL content.
        """
        identity = QuerySetIdentity(name="x", version=1)
        register_query_set(
            db,
            identity,
            (QueryRecord(query_id="q1", query_text="original text", stratum="fts-easy"),),
        )
        with pytest.raises(ValueError, match="different content"):
            register_query_set(
                db,
                identity,
                (QueryRecord(query_id="q1", query_text="DIFFERENT", stratum="fts-easy"),),
            )
        loaded = get_query_set(db, identity)
        assert loaded is not None
        assert loaded.queries[0].query_text == "original text"


# ---------------------------------------------------------------------------
# Row-ID namespacing — ${querySetId}::${queryId} on write, stripped on read
# ---------------------------------------------------------------------------


class TestRowIdNamespacing:
    """09-01 acceptance: row IDs are stored namespaced (raw SQL proof);
    reads strip the prefix (round-trip proof)."""

    def test_row_query_id_stored_namespaced(self, db: sqlite3.Connection) -> None:
        """Raw ``SELECT`` confirms ``lcm_eval_query.query_id`` is the
        namespaced ``${querySetId}::${queryId}`` form."""
        identity = QuerySetIdentity(name="set", version=1)
        register_query_set(
            db,
            identity,
            (
                QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),
                QueryRecord(query_id="q2", query_text="B", stratum="fts-medium"),
            ),
        )
        raw_ids = {
            r[0]
            for r in db.execute(
                "SELECT query_id FROM lcm_eval_query WHERE query_set_id = ?",
                ("set@v1",),
            ).fetchall()
        }
        assert raw_ids == {"set@v1::q1", "set@v1::q2"}

    def test_get_query_set_strips_namespace_prefix(self, db: sqlite3.Connection) -> None:
        """``get_query_set`` returns the UN-namespaced ``query_id``,
        even though the row PK carries the prefix."""
        identity = QuerySetIdentity(name="set", version=1)
        register_query_set(
            db,
            identity,
            (QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),),
        )
        loaded = get_query_set(db, identity)
        assert loaded is not None
        assert loaded.queries[0].query_id == "q1"  # stripped, not "set@v1::q1"


# ---------------------------------------------------------------------------
# Content-signature order-independence
# ---------------------------------------------------------------------------


class TestContentSignatureOrderIndependence:
    """TS ``eval-query-set.test.ts:109-118`` — content signature is
    order-independent.

    The acceptance criterion (09-01): "register ``[q1,q2]`` then assert
    content-signature matches register ``[q2,q1]``." We assert this
    observably: re-registering an identity with a shuffled copy is a
    no-op (signatures equal), while genuinely different content raises.
    """

    def test_shuffled_copy_matches_signature(self, db: sqlite3.Connection) -> None:
        identity = QuerySetIdentity(name="sig", version=1)
        q1 = QueryRecord(
            query_id="q1",
            query_text="first",
            stratum="fts-easy",
            expected_summary_ids=("s2", "s1"),
        )
        q2 = QueryRecord(query_id="q2", query_text="second", stratum="paraphrastic")
        register_query_set(db, identity, (q1, q2))
        # [q2, q1] — same content, reversed order → signature must match.
        register_query_set(db, identity, (q2, q1))  # no-op, no raise
        loaded = get_query_set(db, identity)
        assert loaded is not None
        assert len(loaded.queries) == 2

    def test_expected_ids_order_does_not_affect_signature(self, db: sqlite3.Connection) -> None:
        """The per-query signature sorts ``expected_summary_ids`` —
        ``("s1","s2")`` and ``("s2","s1")`` are the same content."""
        identity = QuerySetIdentity(name="sig", version=1)
        register_query_set(
            db,
            identity,
            (
                QueryRecord(
                    query_id="q1",
                    query_text="x",
                    stratum="fts-easy",
                    expected_summary_ids=("s1", "s2"),
                ),
            ),
        )
        # Same query, expected IDs in the opposite order → no-op.
        register_query_set(
            db,
            identity,
            (
                QueryRecord(
                    query_id="q1",
                    query_text="x",
                    stratum="fts-easy",
                    expected_summary_ids=("s2", "s1"),
                ),
            ),
        )
        loaded = get_query_set(db, identity)
        assert loaded is not None
        assert len(loaded.queries) == 1


# ---------------------------------------------------------------------------
# Corrupt expected_sources JSON is tolerated
# ---------------------------------------------------------------------------


class TestCorruptExpectedSources:
    """09-01 acceptance: "``get_query_set`` tolerates corrupt
    ``expected_sources`` JSON (returns ``expected_summary_ids=None``,
    never raises)."."""

    def test_corrupt_json_read_as_none(self, db: sqlite3.Connection) -> None:
        """A row whose ``expected_sources`` is invalid JSON reads back
        with ``expected_summary_ids is None`` — no exception."""
        identity = QuerySetIdentity(name="set", version=1)
        register_query_set(
            db,
            identity,
            (QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),),
        )
        # Corrupt the stored JSON directly.
        db.execute(
            "UPDATE lcm_eval_query SET expected_sources = ? WHERE query_id = ?",
            ("{not valid json", "set@v1::q1"),
        )
        loaded = get_query_set(db, identity)
        assert loaded is not None
        assert loaded.queries[0].expected_summary_ids is None

    def test_non_list_json_read_as_none(self, db: sqlite3.Connection) -> None:
        """Syntactically valid JSON that is not a list (e.g. an object)
        is also treated as missing — ``expected_summary_ids`` must be a
        list of IDs or nothing."""
        identity = QuerySetIdentity(name="set", version=1)
        register_query_set(
            db,
            identity,
            (QueryRecord(query_id="q1", query_text="A", stratum="fts-easy"),),
        )
        db.execute(
            "UPDATE lcm_eval_query SET expected_sources = ? WHERE query_id = ?",
            (json.dumps({"unexpected": "shape"}), "set@v1::q1"),
        )
        loaded = get_query_set(db, identity)
        assert loaded is not None
        assert loaded.queries[0].expected_summary_ids is None
