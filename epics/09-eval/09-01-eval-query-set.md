---
name: Port issue
about: Port `src/eval/query-set.ts` to Python
title: '[epic-09] eval: port query-set.ts → eval/query_set.py'
labels: 'port, epic-09-eval'
---

## Source (TypeScript)

- File: `src/eval/query-set.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 291 LOC
- Function(s)/class(es): `encodeQuerySetId(identity)`, `decodeQuerySetId(id)`, `registerQuerySet(db, identity, queries)`, `getQuerySet(db, identity)`, `listQuerySets(db)`. Internal helpers: `validateStratum`, `queryContentSignature`, `querySetSignature`, `makeRowQueryId`, `stripRowQueryId`. Public types: `Stratum`, `QueryRecord`, `QuerySetIdentity`, `QuerySet`.

## Target (Python)

- File: `src/lossless_hermes/eval/query_set.py`
- Estimated LOC: ~290 (Python is similar — straightforward dataclass+function port)

## Background

Query sets are the addressable, append-only fixture corpus that the recall + judge harnesses run against. The TS source documents three load-bearing schema-vs-spec mismatches at the top of the file — the port MUST preserve all three to keep cache/run semantics intact:

1. **`query_set_id` encoding.** Schema PK is single TEXT; spec identity is `(name, version)`. Encoded as `name@vN` (separator `@v`, version is a positive integer). Pure-function port; tests assert round-trip equality.
2. **`expected_topics` + `rubric` are `NOT NULL`.** TS writes `'[]'` and `'{"absolute":[],"pairwise":[]}'` placeholders — preserve verbatim so future schema readers don't see surprise nulls.
3. **`lcm_eval_query.query_id` is a global PK.** TS namespaces it as `${querySetId}::${queryId}` on write and strips on read. Port must produce byte-identical row IDs so a TS-seeded DB + Python reader work (or vice versa) without re-seeding.

**Idempotency contract:** `register_query_set` is idempotent on `(identity, content)` — re-register same identity + same content → no-op; same identity + different content → raise (force a new version). Append-only by design. Content signature is order-independent JSON over `(queryId, queryText, stratum, referenceSummary, expectedSummaryIds-sorted)`.

## Python public API

```python
from dataclasses import dataclass
from typing import Literal

Stratum = Literal["fts-easy", "fts-medium", "paraphrastic"]

@dataclass(frozen=True)
class QueryRecord:
    query_id: str
    query_text: str
    stratum: Stratum
    reference_summary: str | None = None
    expected_summary_ids: list[str] | None = None

@dataclass(frozen=True)
class QuerySetIdentity:
    name: str
    version: int  # ≥ 1

@dataclass(frozen=True)
class QuerySet:
    identity: QuerySetIdentity
    queries: list[QueryRecord]

QUERY_SET_ID_SEPARATOR = "@v"
ROW_ID_SEPARATOR = "::"

def encode_query_set_id(identity: QuerySetIdentity) -> str: ...
def decode_query_set_id(id_: str) -> QuerySetIdentity: ...
def register_query_set(conn, identity: QuerySetIdentity, queries: list[QueryRecord]) -> None: ...
def get_query_set(conn, identity: QuerySetIdentity) -> QuerySet | None: ...
def list_query_sets(conn) -> list[QuerySetIdentity]: ...
```

`conn` is the project's `sqlite3.Connection` (sync per ADR-017). `register_query_set` opens a `BEGIN` transaction; on any error rollback and re-raise.

## Dependencies

- **Depends on:** Epic 01-15 (the migration that creates `lcm_eval_query_set` + `lcm_eval_query`).
- **Blocks:** #09-02 (recall needs `QueryRecord` + `Stratum`), #09-03 (judge takes `list[QueryRecord]`), #09-04 (run.py imports `encode_query_set_id`), #09-05 (fixture calls `register_query_set`).

## Acceptance criteria

- [ ] `encode_query_set_id({"name": "eva-baseline", "version": 2}) == "eva-baseline@v2"`; `decode_query_set_id` is the inverse.
- [ ] `encode_query_set_id` raises on empty `name` or non-positive-integer `version`.
- [ ] `decode_query_set_id` rejects strings missing `@v` separator with a clear error.
- [ ] `register_query_set` writes header row + N query rows in one transaction; partial writes never survive a crash mid-loop (test by injecting an `IntegrityError` on the Nth row and asserting empty table).
- [ ] Re-registering the same `(identity, content)` is a no-op (asserted by row count + idempotency-replay test).
- [ ] Re-registering same identity with **different** content raises with a message containing `"already exists with different content"` and naming the encoded ID.
- [ ] Row IDs in `lcm_eval_query.query_id` are stored as `${querySetId}::${queryId}` (asserted by raw SQL `SELECT`); reads strip the prefix and return the un-namespaced `queryId` (asserted by `get_query_set` round-trip).
- [ ] `get_query_set` returns queries sorted by `query_id` ASC for stable iteration.
- [ ] `expected_topics` is written as literal `'[]'`; `rubric` is written as literal `'{"absolute":[],"pairwise":[]}'`.
- [ ] All TS unit tests in `test/eval-query-set.test.ts` (~30 cases) have ported pytest equivalents in `tests/eval/test_query_set.py`.
- [ ] Function signatures match the spec above; types pass `mypy --strict src/lossless_hermes/eval/query_set.py`.
- [ ] `pytest tests/eval/test_query_set.py` passes locally + on GitHub CI matrix.
- [ ] PR description cites the LCM commit SHA being ported (`1f07fbd` for `pr-613` HEAD).

## Tests

Port `test/eval-query-set.test.ts` line-for-line into `tests/eval/test_query_set.py`. Mandatory cases:

- Encode round-trip: `eva-baseline@v2`, `eva-baseline@v7`, names that already contain `@v` (the separator collision test from TS).
- Empty `name` raises.
- Version `0`, negative, non-integer all raise.
- Decode malformed ID raises with helpful message.
- Register empty `queries` list raises.
- Register duplicate `queryId` within a set raises.
- Register invalid stratum raises naming the offending queryId.
- Idempotent re-registration with identical content (different list order) is a no-op.
- Re-registration with content-differing query → raises.
- Order-independence test: register `[q1,q2]` then assert content-signature matches register `[q2,q1]`.
- `get_query_set` returns `None` for missing identity.
- `get_query_set` strips namespace prefix from row IDs.
- `get_query_set` tolerates corrupt `expected_sources` JSON (returns `expected_summary_ids=None`, never raises).
- `list_query_sets` returns identities sorted by `query_set_id` ASC.

## Estimated effort

**4–6 hours.**

## Confidence

**95%** — pure function ports with explicit schema-gap documentation in the TS source comments. Only mild risk: ensuring Python `json.dumps(..., sort_keys=True)` produces byte-identical content signatures to TS's hand-rolled `JSON.stringify({queryId, queryText, ...})` — the TS version uses explicit key order, not sort, so Python should match by listing keys in the same explicit order rather than relying on `sort_keys`.
