"""Embeddings store — per-model vec0 virtual tables for LCM.

Ports ``lossless-claw/src/embeddings/store.ts`` (commit ``1f07fbd`` on
branch ``pr-613``, 609 LOC) to Python. All vec0 interaction in LCM goes
through this module — callers never touch vec0 SQL directly.

### Why centralize all vec0 SQL here

1. **sqlite-vec is best-effort.** The extension may not be loadable in
   every environment (CI without the wheel, dev box with a custom Python,
   container with extension loading disabled). v4.1.1 A7 amendment:
   graceful degrade — if vec0 is missing, the rest of LCM still works
   (FTS-only retrieval, no semantic recall, lower retrieval quality but
   no crash). All embedding writes become no-ops on those installs.

2. **vec0 schema discipline.** vec0 partition keys, metadata columns,
   and auxiliary columns each have different syntax and UPDATE semantics.
   v4.1.1 noted that "UPDATE on PARTITION KEY corrupts vec0" — we use
   METADATA columns for ``suppressed`` (UPDATE works) and AUXILIARY
   columns for ``embedded_id`` (UPDATE not needed; insert-once).
   Centralizing the SQL here prevents callers from accidentally choosing
   the wrong column class.

3. **Polymorphic ``(embedded_id, embedded_kind)`` shape.** One vec0 table
   per model holds rows for summaries / entities / themes. KNN queries
   return both columns directly — no separate join to a mapping table.
   The :class:`lcm_embedding_meta` sidecar is a parallel "is this thing
   embedded?" index that doesn't need to load the vector.

4. **Triggers, not FK CASCADE.** vec0 corrupts under foreign-key
   constraints (v4.1.1 finding). The AFTER UPDATE / AFTER DELETE triggers
   on ``summaries`` cascade suppression and deletion into the vec0 table
   without FKs — per-model because vec0 SQL doesn't support dynamic
   table-name resolution inside triggers.

### Module-level invariant: never UPDATE partition-key columns in vec0

vec0 v4.1.1 has a documented bug where UPDATE on a partition-key column
(``embedding``) or on an auxiliary column (``+embedded_id``) corrupts the
shadow rowid-mapping. Safe UPDATE targets are METADATA columns only —
``embedded_kind`` and ``suppressed``. For any id-or-kind change, use
:func:`delete_embedding` + :func:`record_embedding` (or
:func:`replace_embedding` which does both). Do **not** introduce a
"convenience" UPDATE on auxiliary / partition columns.

### Python sqlite3 vs node:sqlite simplifications

The TS port has two complications that vanish in Python:

* **No ``candidateVec0Paths`` search.** The TS code searches three
  filesystem candidates for ``vec0.<dylib|so|dll>``. The PyPI
  ``sqlite_vec`` package auto-discovers its bundled extension via
  :func:`sqlite_vec.load`. Per spike 001 §"Recommended Python stack",
  we drop that complexity entirely — :func:`try_load_sqlite_vec` collapses
  to ~10 LOC.

* **No BigInt dance.** The TS port casts integers as ``BigInt`` literals
  (``1n`` / ``0n``) before binding because ``node:sqlite`` rejects JS
  ``number`` 0 as FLOAT for vec0's INTEGER metadata columns. Python's
  stdlib ``sqlite3`` uses ``sqlite3_bind_int64`` natively for any
  ``int`` — pass ``1`` / ``0`` directly. Spike 001 §"INTEGER/INT64
  binding" verified this round-trips ``2**62`` cleanly. Do not
  reintroduce BigInt-style binding in Python.

### Vector binding

Per spike 001 §"Performance sanity", binding the vector as raw bytes
via :func:`sqlite_vec.serialize_float32` is ~2.3× faster on insert than
the JSON-string path (``json.dumps(list(vector))``). Both work for
``WHERE embedding MATCH ?``. We use the bytes path everywhere — the
TS code uses JSON for ``node:sqlite``-binding-stability reasons that
don't apply in Python.

See:

* ``docs/porting-guides/embeddings.md`` §"sqlite-vec store"
* ``docs/spike-results/001-sqlite-vec-python.md`` — extension loading PASS
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-4 + Wave-5 SAVEPOINT
  preservation
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence, Union

import sqlite_vec

from lossless_hermes.db.connection import Connection

__all__ = [
    "MAX_EMBEDDING_DIM",
    "MIN_EMBEDDING_DIM",
    "MODEL_NAME_PATTERN",
    "EmbeddedKind",
    "SearchHit",
    "SearchSimilarOptions",
    "delete_embedding",
    "drop_embeddings_triggers",
    "embeddings_table_exists",
    "embeddings_table_name",
    "ensure_embeddings_table",
    "is_embedded",
    "mark_embedding_suppressed",
    "record_embedding",
    "register_embedding_profile",
    "replace_embedding",
    "search_similar",
]

_log = logging.getLogger("lossless_hermes.embeddings.store")


# ---------------------------------------------------------------------------
# Constants + type aliases
# ---------------------------------------------------------------------------

# Allowed model-name shape for use in ``lcm_embeddings_<slug>`` table names.
# SQL identifiers don't accept arbitrary strings; we sanitize aggressively
# and reject anything outside ``[A-Za-z0-9._-]`` (length 1-64). After the
# sluggify step strips non-alphanumeric, only ``[a-z0-9]`` remains. Doubles
# as defense-in-depth against SQL-identifier injection (no bind params are
# allowed in ``CREATE VIRTUAL TABLE`` or ``CREATE TRIGGER``).
# Ports ``store.ts:49`` ``MODEL_NAME_PATTERN``.
MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# vec0 minimum / maximum supported dimension. The upper bound 4096 matches
# ``store.ts:211`` and ``store.ts:307`` — every Voyage model is well under
# this (voyage-3-lite 512, voyage-4-large 1024, voyage-3-large 2048). The
# lower bound 1 catches "0" and negative values; the type check catches
# floats.
MIN_EMBEDDING_DIM = 1
MAX_EMBEDDING_DIM = 4096

# Valid ``embedded_kind`` values. Matches ``store.ts:353``
# ``export type EmbeddedKind = "summary" | "entity" | "theme"``. Anything
# else is rejected at the call site — no silent fallthrough that would
# write a row with an unknown kind into the polymorphic vec0 table.
EmbeddedKind = Literal["summary", "entity", "theme"]

_EMBEDDED_KINDS: tuple[str, ...] = ("summary", "entity", "theme")

# vec0 hard-rejects a "k" of 0 in MATCH queries; values above 1000 are
# theoretical (vec0 is a brute-force scanner, so larger k just means
# slower KNN). Matches ``store.ts:547-549``.
_MIN_K = 1
_MAX_K = 1000


# ---------------------------------------------------------------------------
# Slug normalization (ports ``store.ts:58-72``)
# ---------------------------------------------------------------------------


def embeddings_table_name(model_name: str) -> str:
    """Return the ``lcm_embeddings_<slug>`` table name for ``model_name``.

    Ports ``store.ts:58-72`` ``embeddingsTableName``. The slug is
    lowercase + alphanumeric only. ``voyage-4-large`` → ``voyage4large``,
    ``voyage.4_large-test`` → ``voyage4largetest``.

    Used in ``CREATE VIRTUAL TABLE`` / ``CREATE TRIGGER`` SQL — no bind
    params are allowed in DDL, so the regex sanitization is the defense
    against SQL-identifier injection.

    Raises:
        ValueError: ``model_name`` is empty, longer than 64 chars, or
            contains characters outside ``[A-Za-z0-9._-]``.
        ValueError: ``model_name`` sluggifies to the empty string (e.g.
            an all-symbol input like ``___`` or ``...``).
    """
    if not MODEL_NAME_PATTERN.match(model_name):
        raise ValueError(
            f"[embeddings.store] invalid model name: {model_name!r} "
            f"(must match {MODEL_NAME_PATTERN.pattern}; got len={len(model_name)})"
        )
    slug = re.sub(r"[^a-z0-9]", "", model_name.lower())
    if not slug:
        raise ValueError(
            f"[embeddings.store] model name {model_name!r} sluggifies to empty - "
            "pick a different model name"
        )
    return f"lcm_embeddings_{slug}"


def _slug_for(model_name: str) -> str:
    """Return the bare slug (no ``lcm_embeddings_`` prefix) for trigger names.

    Used in ``CREATE TRIGGER`` SQL so the trigger names follow the
    ``lcm_embed_<verb>_<slug>`` convention.
    """
    return re.sub(r"[^a-z0-9]", "", model_name.lower())


# ---------------------------------------------------------------------------
# vec0 virtual-table + trigger DDL (ports ``store.ts:206-275``)
# ---------------------------------------------------------------------------


def _validate_dim(dim: int) -> None:
    """Raise :class:`ValueError` if ``dim`` is not a usable vec0 dimension.

    Ports the dim-validation block from ``store.ts:211-213`` and ``:302-309``.
    Both call sites use the same upper bound (4096) per Group B Gap 8.
    """
    # ``bool`` is a subclass of ``int`` in Python — guard against it
    # explicitly so ``register_embedding_profile(db, "x", True)`` doesn't
    # silently create a dim-1 table.
    if isinstance(dim, bool) or not isinstance(dim, int):
        raise ValueError(f"[embeddings.store] invalid dim {dim!r} (must be a positive int)")
    if dim < MIN_EMBEDDING_DIM or dim > MAX_EMBEDDING_DIM:
        raise ValueError(
            f"[embeddings.store] invalid dim {dim} "
            f"(must be {MIN_EMBEDDING_DIM}-{MAX_EMBEDDING_DIM})"
        )


def ensure_embeddings_table(conn: Connection, model_name: str, dim: int) -> None:
    """Create the per-model vec0 virtual table + cascade triggers if absent.

    Ports ``store.ts:206-253`` ``ensureEmbeddingsTable``. The schema is:

    .. code-block:: sql

        CREATE VIRTUAL TABLE IF NOT EXISTS lcm_embeddings_<slug> USING vec0(
            embedding float[<dim>],
            +embedded_id text,      -- AUXILIARY: not WHERE-filterable
            embedded_kind text,     -- METADATA: filterable inside MATCH
            suppressed integer      -- METADATA: filterable pre-pass (0/1)
        );

    Column class choice is load-bearing (see ``store.ts:172-180`` comments
    and the module docstring §"vec0 schema discipline"):

    * ``embedding`` — partition key, stores the vector. Distance metric is
      L2 by default; for unit-normalized Voyage vectors this is monotone
      with cosine (``L² = 2·(1 - cos)``).
    * ``+embedded_id`` — auxiliary (``+`` prefix). Stored alongside the
      vector, returned in KNN results, NOT filterable in MATCH WHERE.
    * ``embedded_kind`` — metadata. WHERE-filterable inside MATCH:
      ``WHERE embedded_kind IN ('summary')``. Required for polymorphic
      kind filtering at query time.
    * ``suppressed`` — metadata. Pre-filter so suppressed rows never
      surface in KNN; cheaper than a JOIN to ``summaries``.

    Also creates two per-model triggers on ``summaries``:

    * ``lcm_embed_suppress_<slug>`` — AFTER UPDATE OF ``suppressed_at``,
      cascades the NULL-ness flip into ``lcm_embeddings_<slug>.suppressed``.
    * ``lcm_embed_delete_<slug>`` — AFTER DELETE ON ``summaries``,
      cascades row deletion.

    **Why triggers and not FK CASCADE:** vec0 corrupts under foreign-key
    constraints (v4.1.1 finding). Triggers are the only safe cascade path.
    Per-model because vec0 SQL doesn't support dynamic table-name
    resolution inside triggers.

    Idempotent — uses ``IF NOT EXISTS`` on both the table and the triggers.

    Raises:
        ValueError: ``model_name`` fails the slug rules or ``dim`` is
            outside ``[1, 4096]``.
        sqlite3.OperationalError: sqlite-vec extension not loaded on
            ``conn`` (caller should gate via :func:`vec0_version`).
    """
    _validate_dim(dim)
    table_name = embeddings_table_name(model_name)
    slug = _slug_for(model_name)

    # No bind params allowed in CREATE VIRTUAL TABLE / CREATE TRIGGER.
    # ``table_name`` is validated via ``embeddings_table_name``
    # (alphanumeric only) — defense-in-depth against identifier injection.
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} USING vec0("
        f"  embedding float[{dim}],"
        f"  +embedded_id text,"
        f"  embedded_kind text,"
        f"  suppressed integer"
        f")"
    )

    # Per-model suppression cascade trigger. Fires only when ``suppressed_at``
    # actually transitioned NULL ↔ not-NULL — avoids unnecessary work when
    # other columns of ``summaries`` are updated. Only cascades for
    # ``embedded_kind = 'summary'`` rows; entities/themes have their own
    # suppression paths handled in later epics.
    conn.execute(
        f"CREATE TRIGGER IF NOT EXISTS lcm_embed_suppress_{slug} "
        f"AFTER UPDATE OF suppressed_at ON summaries "
        f"WHEN (NEW.suppressed_at IS NULL) != (OLD.suppressed_at IS NULL) "
        f"BEGIN "
        f"  UPDATE {table_name} "
        f"    SET suppressed = CASE WHEN NEW.suppressed_at IS NULL THEN 0 ELSE 1 END "
        f"    WHERE embedded_id = NEW.summary_id AND embedded_kind = 'summary'; "
        f"END"
    )

    # Per-model deletion cascade trigger. Fires on hard-delete of a summary
    # (only path: lcm_purge with --immediate, or migration cleanup). Removes
    # the vec0 row so KNN doesn't return a dangling pointer.
    conn.execute(
        f"CREATE TRIGGER IF NOT EXISTS lcm_embed_delete_{slug} "
        f"AFTER DELETE ON summaries "
        f"BEGIN "
        f"  DELETE FROM {table_name} "
        f"    WHERE embedded_id = OLD.summary_id AND embedded_kind = 'summary'; "
        f"END"
    )


def drop_embeddings_triggers(
    conn: Connection, model_name: str, *, drop_table: bool = False
) -> None:
    """Drop the per-model triggers (and optionally the vec0 table).

    Ports ``store.ts:263-275`` ``dropEmbeddingsTriggers``. Used during model
    archival / cutover. When ``drop_table=True`` the vec0 virtual table is
    also dropped — unrecoverable. Default is to keep the table for forensic
    queries even after archival; only the ``active`` flag flips in
    ``lcm_embedding_profile``.
    """
    table_name = embeddings_table_name(model_name)
    slug = _slug_for(model_name)
    conn.execute(f"DROP TRIGGER IF EXISTS lcm_embed_suppress_{slug}")
    conn.execute(f"DROP TRIGGER IF EXISTS lcm_embed_delete_{slug}")
    if drop_table:
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")


# ---------------------------------------------------------------------------
# Profile registration (ports ``store.ts:294-351``)
# ---------------------------------------------------------------------------


def register_embedding_profile(conn: Connection, model_name: str, dim: int) -> None:
    """Register an embedding model in ``lcm_embedding_profile``.

    Ports ``store.ts:294-351`` ``registerEmbeddingProfile``. ``INSERT OR
    IGNORE`` makes this idempotent on ``(model_name, dim)`` match — calling
    twice with the same arguments is a no-op. Calling with a different
    ``dim`` for the same model raises :class:`ValueError` (profiles are
    immutable; bump ``model_name`` to switch dimensions).

    **Slug-collision guard (Group B Gap 2):** before inserting, scan
    existing profiles and reject if a *different* ``model_name`` already
    sluggifies to the same value. Two profiles cannot share a vec0 table
    name — silent acceptance would route inserts for both models into the
    same ``lcm_embeddings_<slug>`` table and corrupt KNN results.

    Raises:
        ValueError: ``model_name`` fails :data:`MODEL_NAME_PATTERN`.
        ValueError: ``dim`` is outside ``[1, 4096]`` (the upper bound
            matches :func:`ensure_embeddings_table` per Group B Gap 8).
        ValueError: An existing profile has the same ``model_name`` but a
            different ``dim`` (dim mismatch).
        ValueError: An existing profile has a different ``model_name`` but
            the same slug (collision).
        sqlite3.OperationalError: ``lcm_embedding_profile`` table doesn't
            exist (caller must run migrations first).
    """
    if not MODEL_NAME_PATTERN.match(model_name):
        raise ValueError(
            f"[embeddings.store] invalid model name: {model_name!r} "
            f"(must match {MODEL_NAME_PATTERN.pattern})"
        )
    _validate_dim(dim)

    # Group B Gap 2 fix: check slug uniqueness BEFORE inserting. Compute
    # the slug for the incoming model_name, scan existing rows, throw if
    # a different model_name already has the same slug (would cause a
    # vec0 table-name collision).
    our_slug = re.sub(r"[^a-z0-9]", "", model_name.lower())
    cur = conn.execute(
        "SELECT model_name FROM lcm_embedding_profile WHERE model_name != ?",
        (model_name,),
    )
    for (other_name,) in cur.fetchall():
        other_slug = re.sub(r"[^a-z0-9]", "", other_name.lower())
        if other_slug == our_slug:
            raise ValueError(
                f"[embeddings.store] slug collision: model_name {model_name!r} "
                f"sluggifies to {our_slug!r} which is already used by "
                f"registered model {other_name!r}. Two profiles cannot share "
                "a vec0 table name. Pick a model_name that sluggifies "
                "differently."
            )

    # INSERT OR IGNORE: if a row exists with the same model_name, leave it
    # alone. The dim-match check happens next.
    #
    # No explicit commit() here. The TS port (``store.ts:294-351``) doesn't
    # commit either — node:sqlite is autocommit-by-default for ``exec``,
    # and Python sqlite3's deferred-transaction model means callers wrapping
    # this in their own ``BEGIN``/``COMMIT`` block would be broken by a
    # premature inner commit. Per the function docstring, callers manage
    # the outer transaction; the SELECT readback below sees the uncommitted
    # row via SQLite's read-your-writes guarantee inside the same conn.
    conn.execute(
        "INSERT OR IGNORE INTO lcm_embedding_profile (model_name, dim, active) VALUES (?, ?, 1)",
        (model_name, dim),
    )

    # Defensive: if a profile exists with a different ``dim``, that's a
    # bug — dim is locked at first registration. Silent acceptance of a
    # mismatched dim would corrupt the vec0 table.
    row = conn.execute(
        "SELECT dim FROM lcm_embedding_profile WHERE model_name = ?",
        (model_name,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"[embeddings.store] failed to register profile {model_name!r}")
    existing_dim = row[0]
    if existing_dim != dim:
        raise ValueError(
            f"[embeddings.store] dim mismatch for {model_name!r}: existing "
            f"profile has dim={existing_dim}, caller passed dim={dim}. "
            "Profiles are immutable; bump model_name (e.g. add suffix) "
            "instead."
        )


# ---------------------------------------------------------------------------
# Embedding I/O (ports ``store.ts:366-501``)
# ---------------------------------------------------------------------------


def _validate_kind(embedded_kind: str) -> None:
    """Raise :class:`ValueError` if ``embedded_kind`` is not a valid kind.

    Defensive guard at the public API boundary — the ``EmbeddedKind``
    :class:`typing.Literal` catches typos at static-check time, but a
    stray :class:`str` at runtime would otherwise silently write a row
    with an unknown kind into the polymorphic vec0 table.
    """
    if embedded_kind not in _EMBEDDED_KINDS:
        raise ValueError(
            f"[embeddings.store] invalid embedded_kind {embedded_kind!r} "
            f"(must be one of {_EMBEDDED_KINDS})"
        )


def _serialize_vector(vector: Union[Sequence[float], bytes], dim: int) -> bytes:
    """Return ``vector`` as a vec0-compatible bytes payload.

    Accepts:

    * :class:`bytes` — already-serialized via :func:`sqlite_vec.serialize_float32`
      (caller did the work; we validate the byte length matches ``4 * dim``).
    * :class:`Sequence` of floats (list, tuple, :class:`array.array`, etc.) —
      serialize via :func:`sqlite_vec.serialize_float32`.

    Per spike 001 §"Performance sanity", the bytes path is ~2.3× faster
    than the JSON-string path on insert. The TS code uses JSON for
    node:sqlite-binding-stability reasons that don't apply in Python.
    """
    if isinstance(vector, (bytes, bytearray, memoryview)):
        # Already serialized — validate length and pass through.
        expected_len = 4 * dim
        actual_len = len(vector)
        if actual_len != expected_len:
            raise ValueError(
                f"[embeddings.store] pre-serialized vector has {actual_len} "
                f"bytes, expected {expected_len} (4 * dim={dim}). Likely the "
                "wrong model or a corrupted vector."
            )
        return bytes(vector)

    # Sequence of floats — validate dim then serialize.
    if not hasattr(vector, "__len__"):
        raise TypeError(
            f"[embeddings.store] vector must be a Sequence of floats or "
            f"pre-serialized bytes, got {type(vector).__name__}"
        )
    actual_dim = len(vector)
    if actual_dim != dim:
        raise ValueError(
            f"[embeddings.store] dim mismatch: vector has {actual_dim} elements, profile dim={dim}"
        )
    return sqlite_vec.serialize_float32(list(vector))


def record_embedding(
    conn: Connection,
    *,
    model_name: str,
    embedded_id: str,
    embedded_kind: EmbeddedKind,
    vector: Union[Sequence[float], bytes],
    source_token_count: int,
    suppressed: bool = False,
) -> None:
    """Insert (or replace) an embedding for ``(embedded_id, embedded_kind)``.

    Ports ``store.ts:366-443`` ``recordEmbedding``. Updates BOTH the vec0
    table AND ``lcm_embedding_meta`` atomically — the pair is wrapped in
    a SAVEPOINT so a failure in either rolls back both.

    The caller is responsible for the outer transaction; this function
    does not commit (per ``store.ts:362-365``). The leaf-write path
    embeds inside its existing T1, and the backfill cron groups multiple
    inserts into a single transaction for throughput.

    **Wave-4 Auditor #3 P0 + Wave-5 P1 fixes (preserved verbatim per
    ADR-029):**

    * vec0 auxiliary columns aren't UNIQUE-indexed, so back-to-back
      ``record_embedding`` calls on the same ``(embedded_id, embedded_kind)``
      created DUPLICATE vec0 rows. The meta sidecar self-heals via
      ``INSERT OR REPLACE`` but vec0 accumulates duplicates that surface
      as duplicate KNN hits at search time. Defense-in-depth:
      DELETE-before-INSERT inside a SAVEPOINT.

    * Wave-5: SAVEPOINT name uses :func:`secrets.token_hex` (was
      ``Math.random`` 24-bit in TS pre-Wave-5 — collision-risky under
      concurrent outer-tx callers). 16 hex chars = 64 bits, collision-free
      for any realistic concurrency.

    Raises:
        ValueError: ``model_name`` fails the slug rules.
        ValueError: ``embedded_kind`` is not one of ``("summary", "entity",
            "theme")``.
        ValueError: No profile is registered for ``model_name`` (call
            :func:`register_embedding_profile` first).
        ValueError: ``vector`` length doesn't match the registered profile
            dim, or pre-serialized bytes have the wrong length.
        sqlite3.OperationalError: ``lcm_embeddings_<slug>`` table doesn't
            exist (call :func:`ensure_embeddings_table` first).
    """
    _validate_kind(embedded_kind)
    table_name = embeddings_table_name(model_name)

    # Look up the profile dim. The profile must exist — caller is required
    # to call ``register_embedding_profile`` first (per ``store.ts:381-384``).
    profile_row = conn.execute(
        "SELECT dim FROM lcm_embedding_profile WHERE model_name = ?",
        (model_name,),
    ).fetchone()
    if profile_row is None:
        raise ValueError(
            f"[embeddings.store] no profile registered for {model_name!r} - "
            "call register_embedding_profile first"
        )
    dim = profile_row[0]

    vec_bytes = _serialize_vector(vector, dim)

    # Python sqlite3 simplification: pass ``0`` / ``1`` directly. The TS
    # port uses ``0n`` / ``1n`` BigInt literals because ``node:sqlite``
    # rejects JS ``number`` 0 as FLOAT for vec0's INTEGER metadata
    # column. Python's sqlite3 uses ``sqlite3_bind_int64`` natively —
    # no BigInt dance needed. Document this so future contributors don't
    # reintroduce the dance.
    suppressed_int = 1 if suppressed else 0

    # LCM Wave-4 (2026-01-12): DELETE-before-INSERT inside a SAVEPOINT.
    # vec0 auxiliary cols aren't UNIQUE-indexed; back-to-back
    # record_embedding(...) calls on the same (embedded_id, embedded_kind)
    # created DUPLICATE vec0 rows that surfaced as duplicate KNN hits.
    # The meta sidecar uses INSERT OR REPLACE so it self-heals, but vec0
    # needs the explicit DELETE. The SAVEPOINT pairs the DELETE with the
    # subsequent INSERT atomically (and with the meta upsert), so a
    # partial failure rolls back the pair.
    # LCM Wave-5 (2026-02-03): SAVEPOINT name uses crypto-random suffix
    # (was Math.random 24-bit, ~1/4096 collision risk under concurrent
    # outer-tx callers). 16 hex chars = 64 bits, collision-free for any
    # realistic concurrency.
    sp = f"sp_emb_{secrets.token_hex(8)}"
    conn.execute(f"SAVEPOINT {sp}")
    try:
        conn.execute(
            f"DELETE FROM {table_name} WHERE embedded_id = ? AND embedded_kind = ?",
            (embedded_id, embedded_kind),
        )
        conn.execute(
            f"INSERT INTO {table_name} (embedding, embedded_id, embedded_kind, suppressed) "
            f"VALUES (?, ?, ?, ?)",
            (vec_bytes, embedded_id, embedded_kind, suppressed_int),
        )
        # Mirror in lcm_embedding_meta — sidecar for "is this thing embedded?"
        # queries that don't need to load the vector.
        conn.execute(
            "INSERT OR REPLACE INTO lcm_embedding_meta "
            "  (embedded_id, embedded_kind, embedding_model, embedded_at, "
            "   source_token_count, archived) "
            "VALUES (?, ?, ?, datetime('now'), ?, 0)",
            (embedded_id, embedded_kind, model_name, source_token_count),
        )
        conn.execute(f"RELEASE {sp}")
    except Exception:
        # Best-effort rollback. If the ROLLBACK TO itself fails (e.g.
        # the SAVEPOINT was already released by a nested call gone wrong),
        # we swallow that secondary failure and re-raise the original
        # exception — same pattern as ``store.ts:439-441``.
        try:
            conn.execute(f"ROLLBACK TO {sp}")
            conn.execute(f"RELEASE {sp}")
        except sqlite3.Error:
            pass
        raise


def replace_embedding(
    conn: Connection,
    *,
    model_name: str,
    embedded_id: str,
    embedded_kind: EmbeddedKind,
    vector: Union[Sequence[float], bytes],
    source_token_count: int,
    suppressed: bool = False,
) -> None:
    """Replace an existing embedding (DELETE + record_embedding).

    Ports ``store.ts:450-459`` ``replaceEmbedding``. Use when the source
    content was regenerated (e.g. leaf re-summarized at higher cap per A.10).

    The implementation defers to :func:`record_embedding`, which already
    does DELETE-before-INSERT (Wave-4 fix). The leading DELETE in the TS
    port was redundant after Wave-4 landed — kept for explicit
    intent-signaling at the API boundary. We mirror that here.
    """
    table_name = embeddings_table_name(model_name)
    conn.execute(
        f"DELETE FROM {table_name} WHERE embedded_id = ? AND embedded_kind = ?",
        (embedded_id, embedded_kind),
    )
    record_embedding(
        conn,
        model_name=model_name,
        embedded_id=embedded_id,
        embedded_kind=embedded_kind,
        vector=vector,
        source_token_count=source_token_count,
        suppressed=suppressed,
    )


def delete_embedding(
    conn: Connection,
    *,
    model_name: str,
    embedded_id: str,
    embedded_kind: EmbeddedKind,
) -> None:
    """Delete an embedding from both vec0 and ``lcm_embedding_meta``.

    Ports ``store.ts:465-477`` ``deleteEmbedding``. Used when a source row
    is hard-deleted by purge (the AFTER DELETE trigger on ``summaries``
    handles automatic cascade; this function is for explicit deletes from
    non-trigger paths).
    """
    _validate_kind(embedded_kind)
    table_name = embeddings_table_name(model_name)
    conn.execute(
        f"DELETE FROM {table_name} WHERE embedded_id = ? AND embedded_kind = ?",
        (embedded_id, embedded_kind),
    )
    conn.execute(
        "DELETE FROM lcm_embedding_meta "
        "  WHERE embedded_id = ? AND embedded_kind = ? AND embedding_model = ?",
        (embedded_id, embedded_kind, model_name),
    )


def mark_embedding_suppressed(
    conn: Connection,
    *,
    model_name: str,
    embedded_id: str,
    embedded_kind: EmbeddedKind,
    suppressed: bool,
) -> None:
    """Update the ``suppressed`` metadata column on a vec0 row.

    Ports ``store.ts:487-501`` ``markEmbeddingSuppressed``. vec0 supports
    UPDATE on METADATA columns but NOT on partition-key or auxiliary
    columns (v4.1.1 corruption finding). This function only updates the
    metadata column ``suppressed`` — never the partition-key column
    ``embedding`` or the auxiliary ``embedded_id``. For id/kind changes,
    use :func:`replace_embedding` (DELETE + INSERT).

    Subsequent ``search_similar`` calls with ``exclude_suppressed=True``
    (the default) will skip the row via the metadata pre-filter.
    """
    _validate_kind(embedded_kind)
    table_name = embeddings_table_name(model_name)
    conn.execute(
        f"UPDATE {table_name} SET suppressed = ? WHERE embedded_id = ? AND embedded_kind = ?",
        (1 if suppressed else 0, embedded_id, embedded_kind),
    )


# ---------------------------------------------------------------------------
# KNN search (ports ``store.ts:503-580``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    """One row returned by :func:`search_similar`.

    Ports ``store.ts:517-521`` ``SearchHit`` interface. ``distance`` is L2
    (Euclidean) — for unit-normalized Voyage vectors this is monotone with
    cosine similarity (``L² = 2·(1 - cos)``). Callers that need cosine for
    thresholding should derive it: ``cos = 1 - distance²/2``.
    """

    embedded_id: str
    embedded_kind: EmbeddedKind
    distance: float


@dataclass(frozen=True)
class SearchSimilarOptions:
    """Keyword-only options for :func:`search_similar`.

    Ports ``store.ts:503-515`` ``SearchSimilarOptions``. We keep the
    dataclass + the kwargs-style :func:`search_similar` signature both
    available — the dataclass is the parity-friendly shape for callers
    porting from TS, the kwargs form is more pythonic for new callers.
    """

    model_name: str
    query_vector: Union[Sequence[float], bytes]
    k: int = 50
    embedded_kinds: Sequence[EmbeddedKind] = ("summary",)
    exclude_suppressed: bool = True


def search_similar(
    conn: Connection,
    *,
    model_name: str,
    query_vector: Union[Sequence[float], bytes],
    k: int = 50,
    embedded_kinds: Sequence[EmbeddedKind] = ("summary",),
    exclude_suppressed: bool = True,
) -> list[SearchHit]:
    """KNN search against the per-model vec0 table.

    Ports ``store.ts:542-580`` ``searchSimilar``. Returns nearest-K rows
    by L2 (Euclidean) distance ascending (smallest = most similar).

    SQL shape::

        SELECT embedded_id, embedded_kind, distance
        FROM lcm_embeddings_<slug>
        WHERE embedding MATCH ?
          AND k = ?
          AND suppressed = 0           -- if exclude_suppressed (default)
          AND embedded_kind IN (...)
        ORDER BY distance

    The vector binds as bytes via :func:`sqlite_vec.serialize_float32` —
    ~2.3× faster than JSON per spike 001 §"Performance sanity". Both forms
    work for MATCH; bytes is the default.

    Args:
        conn: Open SQLite connection with sqlite-vec loaded.
        model_name: The embedding model. Determines which
            ``lcm_embeddings_<slug>`` table is queried.
        query_vector: The KNN query vector. Accepts a sequence of floats
            (list, tuple, :class:`array.array`) or pre-serialized bytes
            from :func:`sqlite_vec.serialize_float32`.
        k: Number of nearest neighbours to return. Range ``[1, 1000]``.
        embedded_kinds: Filter to rows of these kinds only. Default
            ``("summary",)`` — entity / theme retrieval surfaces will pass
            their own filters.
        exclude_suppressed: When :data:`True` (default), excludes rows with
            ``suppressed = 1`` via the vec0 metadata pre-filter. v4.1 §10
            invariant: every retrieval surface MUST suppress by default;
            opt-in to :data:`False` only for operator/admin tools.

    Returns:
        A list of :class:`SearchHit` ordered by ``distance`` ascending. May
        be shorter than ``k`` if the table has fewer matching rows. Returns
        an empty list when ``embedded_kinds`` is empty (no work to do).

    Raises:
        ValueError: ``k`` is outside ``[1, 1000]`` or ``model_name`` fails
            the slug rules.
        ValueError: ``embedded_kinds`` contains an invalid kind.
        sqlite3.OperationalError: ``lcm_embeddings_<slug>`` table doesn't
            exist on this connection (caller should gate via
            :func:`embeddings_table_exists`).
    """
    if not isinstance(k, int) or k < _MIN_K or k > _MAX_K:
        raise ValueError(f"[embeddings.store] invalid k={k} (must be {_MIN_K}-{_MAX_K})")
    # An empty kinds list is a no-op — return [] without touching SQL.
    kinds_list = list(embedded_kinds)
    if not kinds_list:
        return []
    for kind in kinds_list:
        _validate_kind(kind)

    table_name = embeddings_table_name(model_name)

    # Look up dim for the query vector (validates the input shape matches
    # the model's profile before we issue MATCH). The profile may not be
    # registered if a caller is querying a model that was archived; in
    # that case fall through to vec0's native validation by skipping the
    # explicit check.
    profile_row = conn.execute(
        "SELECT dim FROM lcm_embedding_profile WHERE model_name = ?",
        (model_name,),
    ).fetchone()
    if profile_row is None:
        # No profile -> caller is querying an archived/unknown model.
        # vec0's MATCH will surface the dim mismatch with its own error;
        # we don't pre-validate here because there's no dim to compare to.
        # Pass the vector through and let vec0 raise if shape is wrong.
        if isinstance(query_vector, (bytes, bytearray, memoryview)):
            vec_bytes: bytes = bytes(query_vector)
        else:
            vec_bytes = sqlite_vec.serialize_float32(list(query_vector))
    else:
        vec_bytes = _serialize_vector(query_vector, profile_row[0])

    # Build the WHERE clause. ``kindPlaceholders`` is parameterized; the
    # ``suppressedFilter`` is a string literal because it's a fixed-shape
    # branch (not user input).
    kind_placeholders = ",".join("?" for _ in kinds_list)
    suppressed_clause = "AND suppressed = 0 " if exclude_suppressed else ""

    sql = (
        f"SELECT embedded_id, embedded_kind, distance "
        f"FROM {table_name} "
        f"WHERE embedding MATCH ? "
        f"  AND k = ? "
        f"  {suppressed_clause}"
        f"  AND embedded_kind IN ({kind_placeholders}) "
        f"ORDER BY distance"
    )
    params: tuple[object, ...] = (vec_bytes, k, *kinds_list)
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    return [SearchHit(embedded_id=row[0], embedded_kind=row[1], distance=row[2]) for row in rows]


# ---------------------------------------------------------------------------
# Cheap existence / "is-embedded" probes (ports ``store.ts:586-609``)
# ---------------------------------------------------------------------------


def embeddings_table_exists(conn: Connection, model_name: str) -> bool:
    """Does the vec0 virtual table for ``model_name`` exist?

    Ports ``store.ts:586-592`` ``embeddingsTableExists``. Cheap
    ``sqlite_master`` lookup; safe to call when vec0 isn't loaded
    (returns :data:`False`). Used by ``runSemanticSearch`` and ``/lcm
    health`` to gate KNN dispatch.
    """
    table_name = embeddings_table_name(model_name)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row and row[0])


def is_embedded(
    conn: Connection,
    *,
    embedded_id: str,
    embedded_kind: EmbeddedKind,
    model_name: str,
) -> bool:
    """Has this ``(embedded_id, embedded_kind, model_name)`` tuple been embedded?

    Ports ``store.ts:598-609`` ``isEmbedded``. Cheap meta lookup; does NOT
    touch vec0. Used by:

    * The backfill cron's ``NOT EXISTS`` pre-filter to skip already-embedded
      rows (issue 05-07).
    * ``/lcm health`` for backlog counts (Epic 08).

    Only considers ``archived = 0`` rows — archived profile-rows are
    excluded so the embed status reflects the *currently active* embedding
    model.
    """
    _validate_kind(embedded_kind)
    row = conn.execute(
        "SELECT 1 FROM lcm_embedding_meta "
        "  WHERE embedded_id = ? AND embedded_kind = ? "
        "  AND embedding_model = ? AND archived = 0",
        (embedded_id, embedded_kind, model_name),
    ).fetchone()
    return row is not None


# Suppress "unused import" warning for typing imports re-exported as part of
# the public surface via ``__all__``.
_ = Iterable
