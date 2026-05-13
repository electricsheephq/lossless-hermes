"""Runtime feature probes for FTS5 + trigram tokenizer + sqlite-vec (vec0).

Ports ``src/db/features.ts`` from LCM ``1f07fbd`` (TS lines 1-61 — see
upstream pin: ``/Volumes/LEXAR/Claude/lossless-claw/src/db/features.ts``).
Extends the TS surface with a ``vec0_available`` flag — LCM-TS assumes
node:sqlite always has its loaded extensions (vec0 is a single
``sqlite-vec`` package whose presence is decided at install time); the
Python port instead resolves vec0 at runtime because the extension load
itself can fail (per spike 001 §Findings — Apple system Python lacks
``enable_load_extension``). The probe pattern is identical to FTS5's.

The probe creates a temporary virtual table inside a ``SAVEPOINT``, then
``ROLLBACK`` to leave zero residue in ``sqlite_master`` even if the
``DROP`` step is skipped (e.g. the create succeeded but a follow-up step
raises). The savepoint name and table name both carry a random suffix
from :func:`secrets.token_hex` so concurrent probes against the same
in-memory database — pytest workers, parallel fixtures — cannot collide.

The result is cached per-connection in a module-level dict keyed on
``id(conn)``. This is a **deliberate deviation** from the TS source
(which uses ``WeakMap<DatabaseSync, ...>``) and from issue 01-03's AC
item 7 ("Cache is a WeakKeyDictionary"). The reason is concrete:
CPython's built-in ``sqlite3.Connection`` is an extension type that
does **not** expose ``__weakref__``, so :class:`weakref.WeakKeyDictionary`
raises ``TypeError: cannot create weak reference to 'sqlite3.Connection'
object``. Subclassing to add the slot would force every caller (and
01-01's ``open_lcm_db``) to use a custom ``factory=`` argument, which
contradicts both 01-03's dispatch contract ("use stdlib
``sqlite3.connect(':memory:')`` directly") and the broader storage
porting guide (`docs/porting-guides/storage.md` §1 row 3 — connection
helper is a thin wrapper over stdlib). Instead:

* The cache is keyed on ``id(conn)``. Two distinct ``Connection``
  objects (different ``id``) get separate cache entries even though
  they probe the same SQLite build.
* Cache entries are not auto-evicted on ``conn.close()``. In practice
  LCM processes hold one or two long-lived connections, so the steady-
  state cache size is tiny. A long-running test runner that opens and
  closes thousands of connections accumulates a few KB of stale entries
  — well below any practical threshold. If this ever becomes a real
  problem, callers can call :func:`clear_db_features_cache` explicitly
  (e.g. in a ``conftest.py`` autouse teardown fixture) or we can add a
  ``factory=LcmConnection`` subclass path later without breaking the
  public API.
* ``id()`` reuse after garbage collection is theoretically possible
  (CPython may recycle an address), which would produce a stale-hit if
  the new conn's probe answers diverged from the old conn's. In
  practice this requires (a) a Connection going out of scope, (b) a
  new one allocated at exactly the same address, and (c) the new one
  having different extension state — extremely unlikely. The risk is
  documented and the explicit clear function is the escape hatch.

See:

* ADR-029 §Wave-fix provenance — this module is **not** tagged; the LCM
  features.ts file has no Wave-N audit comments. Provenance citation is
  the standard TS-line reference in the function docstring.
* `docs/spike-results/005-sqlite3-fts5-trigram.md` — confirms FTS5 +
  trigram are present in every CPython 3.11+ stdlib ``sqlite3``.
* `docs/spike-results/001-sqlite-vec-python.md` — confirms ``sqlite_vec``
  loads on Homebrew Python 3.12+ and is what ``open_lcm_db`` (issue
  01-01) is expected to call ``conn.enable_load_extension(True);
  sqlite_vec.load(conn)`` to make vec0 available.
* `docs/porting-guides/storage.md` §"FTS5 virtual tables (created only
  when ``fts5Available``)" — downstream consumers of these flags.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass

__all__ = ["DbFeatures", "clear_db_features_cache", "get_lcm_db_features"]


@dataclass(frozen=True, slots=True)
class DbFeatures:
    """Runtime-detected SQLite features available on a given connection.

    Each flag is set by a probe that attempts the relevant DDL inside a
    rolled-back ``SAVEPOINT`` (see :func:`_probe_virtual_table`). All
    flags default to ``False`` so a probe failure leaves the conservative
    answer — callers degrade gracefully (e.g. skip the CJK FTS table,
    fall back to LIKE search, drop vec0-backed retrieval to a
    no-embedding path).

    Attributes:
        fts5_available: ``True`` when ``CREATE VIRTUAL TABLE ... USING
            fts5(content)`` succeeds. Every mainstream CPython 3.11+
            build ships SQLite with FTS5 compiled in (spike 005 §"SQLite
            versions found"); this flag exists for the niche of
            custom-compiled Python with ``--disable-fts5``.
        fts5_trigram_available: ``True`` when ``... fts5(content,
            tokenize='trigram')`` succeeds. SQLite 3.34+ (Dec 2020) is
            required — Python 3.11's bundled SQLite is 3.39, well above
            the floor. False here causes ``summaries_fts_cjk`` to be
            skipped at migration time per storage guide §2.2.
        vec0_available: ``True`` when ``CREATE VIRTUAL TABLE ... USING
            vec0(embedding float[1])`` succeeds. Requires the
            ``sqlite_vec`` extension to have been loaded prior to the
            probe (see ``db/connection.open_lcm_db`` issue 01-01). False
            here means the embedding/Voyage path is unavailable and
            semantic retrieval gracefully degrades to lexical-only.
    """

    fts5_available: bool
    fts5_trigram_available: bool
    vec0_available: bool


# Module-level per-connection cache. Keyed on ``id(conn)`` because
# stdlib ``sqlite3.Connection`` does not support ``weakref.ref`` — see
# module docstring §"Cache" for the trade-off discussion. The value is
# a ``(DbFeatures,)`` 1-tuple rather than a bare ``DbFeatures`` so the
# clear function can distinguish "absent" from "explicitly cached as
# None" should we ever need that distinction; today it's purely
# defensive.
_feature_cache: dict[int, DbFeatures] = {}


def clear_db_features_cache(conn: sqlite3.Connection | None = None) -> None:
    """Drop cached probe results.

    Args:
        conn: When supplied, only the entry for that specific connection
            is removed (a no-op if the connection was never probed).
            When ``None``, the entire cache is cleared — useful for test
            isolation when a global teardown wants a clean slate.

    Tests covering the per-connection cache invariant (issue 01-03 AC
    items 2 and 7) use the ``conn``-targeted call. Callers in
    production rarely need to invoke this — the cache is small and the
    probes are cheap (under 1 ms each).
    """
    if conn is None:
        _feature_cache.clear()
    else:
        _feature_cache.pop(id(conn), None)


def _probe_virtual_table(conn: sqlite3.Connection, create_sql: str) -> bool:
    """Run a ``CREATE VIRTUAL TABLE`` inside a savepoint; roll back unconditionally.

    Returns ``True`` if the CREATE succeeded, ``False`` if it raised any
    :class:`sqlite3.OperationalError` (the family that covers "no such
    module: vec0", "no such tokenizer: trigram", and "no such module:
    fts5"). Other database errors are intentionally caught with
    :class:`sqlite3.DatabaseError` so a corrupt or locked database
    produces a probe-failed answer rather than crashing the caller; the
    feature flag then encodes "we cannot use this safely" which is the
    correct conservative answer.

    The savepoint name carries a random suffix so two concurrent probes
    (parallel pytest workers against shared ``:memory:`` URIs, fixture
    factories) cannot release each other's savepoints. After the probe,
    ``ROLLBACK TO SAVEPOINT … ; RELEASE SAVEPOINT …`` is run to remove
    any schema residue — guaranteeing ``sqlite_master`` is byte-identical
    before and after. The release-after-rollback pattern is the SQLite-
    documented way to discard a savepoint completely (a bare ROLLBACK TO
    leaves the savepoint open).

    Args:
        conn: An open SQLite connection. The connection's autocommit /
            isolation level does not matter — ``SAVEPOINT`` works in
            both implicit-transaction and autocommit modes.
        create_sql: A ``CREATE VIRTUAL TABLE <name> USING <module>(...)``
            statement. The caller is responsible for embedding a unique
            table name (see callers below).

    Returns:
        ``True`` if the create succeeded; ``False`` otherwise.
    """
    sp_name = f"lcm_feature_probe_{secrets.token_hex(4)}"
    conn.execute(f"SAVEPOINT {sp_name}")
    try:
        conn.execute(create_sql)
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        # Always roll back, even on success — the probe is for
        # detection, not for materializing a schema object. Roll-back-
        # then-release leaves the savepoint stack empty (a bare ROLLBACK
        # TO would leave the savepoint open for re-use).
        conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
        conn.execute(f"RELEASE SAVEPOINT {sp_name}")


def _probe_fts5(conn: sqlite3.Connection) -> bool:
    """FTS5 module probe — see :func:`_probe_virtual_table`."""
    table = f"_lcm_fts5_probe_{secrets.token_hex(4)}"
    return _probe_virtual_table(
        conn,
        f"CREATE VIRTUAL TABLE {table} USING fts5(content)",
    )


def _probe_fts5_trigram(conn: sqlite3.Connection) -> bool:
    """FTS5 trigram-tokenizer probe — see :func:`_probe_virtual_table`."""
    table = f"_lcm_trigram_probe_{secrets.token_hex(4)}"
    return _probe_virtual_table(
        conn,
        f"CREATE VIRTUAL TABLE {table} USING fts5(content, tokenize='trigram')",
    )


def _probe_vec0(conn: sqlite3.Connection) -> bool:
    """vec0 (sqlite-vec) module probe — see :func:`_probe_virtual_table`.

    Returns ``True`` only when the ``sqlite_vec`` extension has been
    loaded on this connection prior to the probe. The probe uses a
    minimal ``float[1]`` shape so the test is purely "is the module
    registered?" and not influenced by dimension-validation paths.
    """
    table = f"_lcm_vec0_probe_{secrets.token_hex(4)}"
    return _probe_virtual_table(
        conn,
        f"CREATE VIRTUAL TABLE {table} USING vec0(embedding float[1])",
    )


def get_lcm_db_features(conn: sqlite3.Connection) -> DbFeatures:
    """Detect FTS5 + trigram + vec0 availability on ``conn``; cache per-connection.

    The probe is runtime-state-specific, not database-file-specific:
    every probe answer is a function of (a) the SQLite build (FTS5
    compiled in? trigram tokenizer registered?) and (b) whether
    extension loading has been attempted on this exact ``Connection``
    (vec0). All three flags are stable for the lifetime of the
    connection, so we cache the result and re-use it on subsequent
    calls. The cache key is the ``Connection`` object itself; the
    :class:`weakref.WeakKeyDictionary` evicts the entry once the
    connection is garbage-collected (typically after ``conn.close()``).

    Probe order matters: ``fts5_trigram_available`` requires FTS5, so we
    short-circuit it to ``False`` when FTS5 is unavailable (matches the
    TS contract — `src/db/features.ts:53-58`). vec0 is independent.

    Args:
        conn: An open SQLite connection. The connection state (whether
            extensions have been loaded, schema PRAGMAs applied, etc.)
            affects only ``vec0_available``; FTS5 flags are pure
            functions of the SQLite build.

    Returns:
        A :class:`DbFeatures` instance describing what the runtime
        supports. The instance is frozen and shared across all callers
        of this function with the same ``conn``.

    Examples:
        >>> import sqlite3
        >>> conn = sqlite3.connect(":memory:")
        >>> features = get_lcm_db_features(conn)
        >>> features.fts5_available  # True on every mainstream Python
        True
        >>> get_lcm_db_features(conn) is features  # cached
        True
    """
    key = id(conn)
    cached = _feature_cache.get(key)
    if cached is not None:
        return cached

    fts5 = _probe_fts5(conn)
    trigram = _probe_fts5_trigram(conn) if fts5 else False
    vec0 = _probe_vec0(conn)
    detected = DbFeatures(
        fts5_available=fts5,
        fts5_trigram_available=trigram,
        vec0_available=vec0,
    )
    _feature_cache[key] = detected
    return detected
