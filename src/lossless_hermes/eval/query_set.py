"""Query set management — LCM v4.1 §11 / D.03.

Ports ``lossless-claw/src/eval/query-set.ts`` (LCM commit ``1f07fbd``
on branch ``pr-613``, 292 LOC TS → ~340 LOC Python with docstrings).

Loads + manages a query set in ``lcm_eval_query_set`` + ``lcm_eval_query``
(defined in :mod:`lossless_hermes.db.migration`, v4.1 §11 / A.05).

### Schema notes (verbatim from TS source ``query-set.ts:7-30``)

The schema's PK on ``lcm_eval_query_set`` is a single TEXT column
``query_set_id``, not ``(name, version)``. Per the task spec the
logical identity is ``(name, version)``, so we encode the composite as::

    query_set_id = f"{name}@v{version}"

(this keeps name/version round-trippable without a migration change.)

The schema requires ``expected_topics TEXT NOT NULL`` and
``rubric TEXT NOT NULL`` on each ``lcm_eval_query`` row. The task spec's
:class:`QueryRecord` doesn't surface these — when registering we
serialize :attr:`QueryRecord.expected_summary_ids` (if any) to
``expected_sources`` (JSON), leave ``expected_topics`` as ``'[]'``,
and write a placeholder rubric (``'{"absolute":[],"pairwise":[]}'``).
Group F's ``/lcm eval`` UI can layer richer rubric+topics on top later
by extending :class:`QueryRecord`.

``lcm_eval_query.query_id`` is a GLOBAL primary key (TEXT NOT NULL
PRIMARY KEY across the whole table — not scoped per ``query_set_id``).
That would force callers to invent globally-unique IDs across
versions, which conflicts with the spec's per-set ``queryId`` model.
We solve this by namespacing on write: the row's ``query_id`` is
stored as ``f"{query_set_id}::{queryId}"``. Reads strip the prefix so
callers see the unprefixed ``queryId``.

### Idempotency

:func:`register_query_set` is idempotent on identity: re-registering
the same ``(name, version)`` with the same content is a no-op.
Re-registering with DIFFERENT content raises :class:`ValueError`
(use a new version instead — versions are append-only by design, the
audit trail matters).

See:

* ``epics/08-cli-ops/08-13-eval-runner.md`` — this issue.
* ``lossless-claw/src/eval/query-set.ts:1-292`` — TS source.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal, Optional

__all__ = [
    "QUERY_SET_ID_SEPARATOR",
    "ROW_ID_SEPARATOR",
    "QueryRecord",
    "QuerySet",
    "QuerySetIdentity",
    "Stratum",
    "decode_query_set_id",
    "encode_query_set_id",
    "get_query_set",
    "list_query_sets",
    "register_query_set",
]


Stratum = Literal["fts-easy", "fts-medium", "paraphrastic"]
"""TS ``type Stratum = "fts-easy" | "fts-medium" | "paraphrastic"``
(``query-set.ts:41``). The DB ``lcm_eval_query.stratum`` column has a
matching ``CHECK`` constraint."""


@dataclass(frozen=True, slots=True)
class QueryRecord:
    """Single query in a set.

    Ports the TS ``QueryRecord`` interface (``query-set.ts:43-51``).

    Attributes:
        query_id: Per-set stable id. Namespaced on write to satisfy
            the global PK on ``lcm_eval_query.query_id``.
        query_text: Raw query text.
        stratum: One of ``fts-easy`` / ``fts-medium`` / ``paraphrastic``.
        reference_summary: Optional reference text for synthesis-quality
            scoring (deferred — not used by recall).
        expected_summary_ids: Optional ground-truth retrieval targets
            (summary IDs). Queries without expected IDs are skipped
            from recall aggregation (per ``recall.ts:18-21``).
    """

    query_id: str
    query_text: str
    stratum: Stratum
    reference_summary: Optional[str] = None
    expected_summary_ids: Optional[tuple[str, ...]] = None


@dataclass(frozen=True, slots=True)
class QuerySetIdentity:
    """Composite identity for a query-set version.

    Ports the TS ``QuerySetIdentity`` interface (``query-set.ts:53-58``).

    Attributes:
        name: Stable name — e.g. ``"eva-baseline-v2"``.
        version: Monotone-incrementing per ``name``. Must be a positive
            integer.
    """

    name: str
    version: int


@dataclass(frozen=True, slots=True)
class QuerySet:
    """A registered query set.

    Ports the TS ``QuerySet`` interface (``query-set.ts:60-63``).
    """

    identity: QuerySetIdentity
    queries: tuple[QueryRecord, ...]


QUERY_SET_ID_SEPARATOR = "@v"
"""Separator between name and version in the encoded ``query_set_id``.
Matches TS ``query-set.ts:65``."""

ROW_ID_SEPARATOR = "::"
"""Separator between the encoded ``query_set_id`` and the per-set
``queryId`` in the namespaced ``lcm_eval_query.query_id`` row PK.
Matches TS ``query-set.ts:127``."""


def encode_query_set_id(identity: QuerySetIdentity) -> str:
    """Encode ``(name, version)`` → ``query_set_id``.

    Ports TS ``encodeQuerySetId`` (``query-set.ts:73-81``).

    Names containing ``@v`` get suffixed with a literal so we can still
    round-trip; the encoding is unique by construction.

    Args:
        identity: The query-set identity.

    Returns:
        The encoded ``query_set_id`` of the form ``f"{name}@v{version}"``.

    Raises:
        ValueError: If ``name`` is empty or ``version`` is not a positive
            integer.
    """
    if not identity.name:
        raise ValueError("query set name must be non-empty")
    if not isinstance(identity.version, int) or identity.version < 1:
        raise ValueError(f"query set version must be a positive integer (got {identity.version})")
    return f"{identity.name}{QUERY_SET_ID_SEPARATOR}{identity.version}"


def decode_query_set_id(query_set_id: str) -> QuerySetIdentity:
    """Decode ``query_set_id`` → ``(name, version)``.

    Ports TS ``decodeQuerySetId`` (``query-set.ts:83-95``).

    Args:
        query_set_id: The encoded id.

    Returns:
        The decoded :class:`QuerySetIdentity`.

    Raises:
        ValueError: If the id is malformed (missing separator, empty
            name, or non-numeric version).
    """
    idx = query_set_id.rfind(QUERY_SET_ID_SEPARATOR)
    if idx < 0:
        raise ValueError(
            f"malformed query_set_id (missing {QUERY_SET_ID_SEPARATOR!r}): {query_set_id}"
        )
    name = query_set_id[:idx]
    version_str = query_set_id[idx + len(QUERY_SET_ID_SEPARATOR) :]
    try:
        version = int(version_str)
    except ValueError as exc:
        raise ValueError(f"malformed query_set_id: {query_set_id}") from exc
    if not name:
        raise ValueError(f"malformed query_set_id: {query_set_id}")
    return QuerySetIdentity(name=name, version=version)


def _validate_stratum(s: str, query_id: str) -> Stratum:
    """Ports TS ``validateStratum`` (``query-set.ts:97-102``)."""
    if s not in ("fts-easy", "fts-medium", "paraphrastic"):
        raise ValueError(f"query {query_id} has invalid stratum: {s}")
    return s  # type: ignore[return-value]


def _query_content_signature(q: QueryRecord) -> str:
    """Compute a deterministic content hash for a single query record.

    Ports TS ``queryContentSignature`` (``query-set.ts:104-119``). Used
    to detect "same identity, different content" registration calls.
    """
    expected = sorted(q.expected_summary_ids) if q.expected_summary_ids else None
    ref = q.reference_summary if q.reference_summary is not None else None
    return json.dumps(
        {
            "queryId": q.query_id,
            "queryText": q.query_text,
            "stratum": q.stratum,
            "referenceSummary": ref,
            "expectedSummaryIds": expected,
        },
        sort_keys=False,
        separators=(",", ":"),
    )


def _query_set_signature(queries: tuple[QueryRecord, ...]) -> str:
    """Ports TS ``querySetSignature`` (``query-set.ts:121-125``).

    Order-independent — sorts by ``query_id`` before hashing.
    """
    sorted_q = sorted(queries, key=lambda q: q.query_id)
    return json.dumps([_query_content_signature(q) for q in sorted_q])


def _make_row_query_id(query_set_id: str, query_id: str) -> str:
    """Namespace a query row's primary-key value with its ``query_set_id``.

    Ports TS ``makeRowQueryId`` (``query-set.ts:130-132``).
    """
    return f"{query_set_id}{ROW_ID_SEPARATOR}{query_id}"


def _strip_row_query_id(row_query_id: str, query_set_id: str) -> str:
    """Strip the namespace prefix from a row's ``query_id``.

    Ports TS ``stripRowQueryId`` (``query-set.ts:135-143``).

    Rows that pre-date the namespacing convention (or were inserted by
    a different code path) round-trip unchanged.
    """
    prefix = f"{query_set_id}{ROW_ID_SEPARATOR}"
    if row_query_id.startswith(prefix):
        return row_query_id[len(prefix) :]
    return row_query_id


def register_query_set(
    db: sqlite3.Connection,
    identity: QuerySetIdentity,
    queries: tuple[QueryRecord, ...] | list[QueryRecord],
) -> None:
    """Register a NEW query set version. Idempotent on ``(identity, content)``.

    Ports TS ``registerQuerySet`` (``query-set.ts:153-224``).

    Semantics:

    * If no row exists for this ``query_set_id`` → INSERT both header + queries.
    * If a row exists with IDENTICAL content → no-op.
    * If a row exists with DIFFERENT content → raise :class:`ValueError`
      (use a new version instead).

    Wrapped in a transaction so a half-written set won't survive a crash.

    Args:
        db: SQLite connection. Caller owns transaction state; we run
            our own ``BEGIN``/``COMMIT`` here (same as TS ``db.exec("BEGIN")``).
        identity: The query-set identity.
        queries: The queries to register. Must be non-empty.

    Raises:
        ValueError: On empty set, duplicate ``query_id`` within the set,
            empty ``query_text``, invalid stratum, or
            same-identity-different-content registration.
    """
    queries_tuple = tuple(queries)
    query_set_id = encode_query_set_id(identity)
    if len(queries_tuple) == 0:
        raise ValueError(f"cannot register empty query set {query_set_id}")

    # Validate up-front so we don't half-write.
    seen_ids: set[str] = set()
    for q in queries_tuple:
        if not q.query_id:
            raise ValueError(f"query missing query_id in set {query_set_id}")
        if q.query_id in seen_ids:
            raise ValueError(f"duplicate query_id {q.query_id} in set {query_set_id}")
        seen_ids.add(q.query_id)
        _validate_stratum(q.stratum, q.query_id)
        if not q.query_text:
            raise ValueError(f"query {q.query_id} has empty query_text")

    existing = get_query_set(db, identity)
    if existing is not None:
        a = _query_set_signature(existing.queries)
        b = _query_set_signature(queries_tuple)
        if a != b:
            raise ValueError(
                f"query set {query_set_id} already exists with different content; "
                f"register a new version instead of mutating an existing one"
            )
        return  # idempotent: same content, no-op.

    # ADR-017: sync-only DB surface; explicit BEGIN/COMMIT.
    db.execute("BEGIN")
    try:
        db.execute(
            """
            INSERT INTO lcm_eval_query_set (query_set_id, version, description)
            VALUES (?, ?, ?)
            """,
            (query_set_id, identity.version, None),
        )
        for q in queries_tuple:
            db.execute(
                """
                INSERT INTO lcm_eval_query
                  (query_id, query_set_id, query_text, stratum,
                   expected_topics, expected_sources, reference_summary,
                   must_not_regress, rubric)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _make_row_query_id(query_set_id, q.query_id),
                    query_set_id,
                    q.query_text,
                    q.stratum,
                    # expected_topics is NOT NULL — empty JSON array placeholder.
                    "[]",
                    (json.dumps(list(q.expected_summary_ids)) if q.expected_summary_ids else None),
                    q.reference_summary,
                    0,
                    # rubric is NOT NULL — placeholder pointing at the
                    # absolute+pairwise shape from architecture-v4.1 §11.
                    '{"absolute":[],"pairwise":[]}',
                ),
            )
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # swallow rollback error, surface the original.
        raise


def get_query_set(
    db: sqlite3.Connection,
    identity: QuerySetIdentity,
) -> Optional[QuerySet]:
    """Look up a query set by identity. Returns ``None`` if it doesn't exist.

    Ports TS ``getQuerySet`` (``query-set.ts:232-279``).

    Queries are returned in ``query_id`` order so callers can rely on a
    stable iteration order across reads.

    Args:
        db: SQLite connection.
        identity: The query-set identity.

    Returns:
        The :class:`QuerySet` or ``None`` if not registered.
    """
    query_set_id = encode_query_set_id(identity)
    header_row = db.execute(
        "SELECT query_set_id, version FROM lcm_eval_query_set WHERE query_set_id = ?",
        (query_set_id,),
    ).fetchone()
    if header_row is None:
        return None

    rows = db.execute(
        """
        SELECT query_id, query_text, stratum, expected_sources, reference_summary
          FROM lcm_eval_query
          WHERE query_set_id = ?
          ORDER BY query_id ASC
        """,
        (query_set_id,),
    ).fetchall()

    queries: list[QueryRecord] = []
    for r in rows:
        raw_query_id = _strip_row_query_id(r[0], query_set_id)
        expected_summary_ids: Optional[tuple[str, ...]] = None
        if r[3] is not None:
            try:
                parsed = json.loads(r[3])
                if isinstance(parsed, list):
                    expected_summary_ids = tuple(str(s) for s in parsed)
            except (json.JSONDecodeError, TypeError):
                # Tolerate corrupt JSON — treat as missing.
                expected_summary_ids = None

        queries.append(
            QueryRecord(
                query_id=raw_query_id,
                query_text=r[1],
                stratum=_validate_stratum(r[2], raw_query_id),
                reference_summary=r[4] if r[4] is not None else None,
                expected_summary_ids=expected_summary_ids,
            )
        )

    return QuerySet(
        identity=decode_query_set_id(header_row[0]),
        queries=tuple(queries),
    )


def list_query_sets(db: sqlite3.Connection) -> list[QuerySetIdentity]:
    """List all registered query sets, sorted by name then version ASC.

    Ports TS ``listQuerySets`` (``query-set.ts:284-291``).

    The latest version of each name is last in the list.

    Args:
        db: SQLite connection.

    Returns:
        Sorted list of identities. Empty if no sets are registered.
    """
    rows = db.execute(
        "SELECT query_set_id FROM lcm_eval_query_set ORDER BY query_set_id ASC"
    ).fetchall()
    return [decode_query_set_id(r[0]) for r in rows]
