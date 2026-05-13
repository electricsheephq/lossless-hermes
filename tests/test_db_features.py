"""Tests for ``lossless_hermes.db.features`` — runtime FTS5 / trigram / vec0 probes.

Validates issue 01-03 AC:

* Probes return correct flags on a stock stdlib ``sqlite3`` connection
  (FTS5 + trigram both ``True`` per spike 005 §"SQLite versions found";
  vec0 ``True`` after ``sqlite_vec.load(conn)`` per spike 001 §Findings).
* The cache returns the same ``DbFeatures`` object on a repeated call
  with the same connection (no re-probe), and a different object for a
  fresh connection.
* :func:`clear_db_features_cache` is the explicit invalidation hook —
  honors the spirit of the TS ``WeakKeyDictionary`` contract under the
  stdlib ``sqlite3.Connection`` constraint that ``__weakref__`` is
  unsupported. See ``src/lossless_hermes/db/features.py`` §"Cache".
* Negative path: when the trigram probe raises ``OperationalError``,
  ``fts5_trigram_available`` is ``False`` and ``fts5_available`` stays
  whatever the FTS5 probe returned (no spurious clearing).
* The probe leaves zero schema residue (``sqlite_master`` snapshot is
  byte-identical before and after).

The tests deliberately use ``sqlite3.connect(':memory:')`` directly —
the ``db.connection.open_lcm_db`` helper from issue 01-01 hasn't landed
yet (the AC list explicitly calls out this independence), and the vec0
test imports ``sqlite_vec`` and loads it inline.

References:

* `src/lossless_hermes/db/features.py` — implementation under test
* `epics/01-storage/01-03-db-features.md` — issue spec + AC list
* `docs/spike-results/005-sqlite3-fts5-trigram.md` — empirical FTS5
  ground truth for stdlib ``sqlite3``
* `docs/spike-results/001-sqlite-vec-python.md` — empirical vec0
  ground truth for ``sqlite_vec.load(conn)``
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

import pytest
import sqlite_vec

from lossless_hermes.db.features import (
    DbFeatures,
    _feature_cache,
    clear_db_features_cache,
    get_lcm_db_features,
)


@pytest.fixture(autouse=True)
def _clear_features_cache_around_each_test() -> Iterator[None]:
    """Module-level cache is shared across tests — clear it before and
    after each test so per-test fixtures see a cold cache and so that
    no test leaks state into the next.
    """
    clear_db_features_cache()
    try:
        yield
    finally:
        clear_db_features_cache()


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """A bare ``:memory:`` connection — no extension loading, no PRAGMAs.

    Mirrors the ``db_in_memory`` conftest fixture so the seam is the
    same shape, but doesn't import it (deliberate decoupling per the
    01-03 spec — these tests must work without ``open_lcm_db``).
    """
    c = sqlite3.connect(":memory:")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def conn_with_vec0() -> Iterator[sqlite3.Connection]:
    """A ``:memory:`` connection with the ``sqlite_vec`` extension loaded.

    Skips when ``enable_load_extension`` is unavailable (Apple system
    Python — see spike 001 §Findings) — but Homebrew Python and every
    Linux mainstream build have it. CI runs Homebrew Python on macOS
    and python:3.x-slim on Linux, both of which expose the attribute.
    """
    c = sqlite3.connect(":memory:")
    try:
        if not hasattr(c, "enable_load_extension"):
            pytest.skip("This Python build has no enable_load_extension (spike 001).")
        try:
            c.enable_load_extension(True)
        except sqlite3.NotSupportedError:
            pytest.skip("Python compiled without loadable extensions (spike 001 §Gotchas).")
        sqlite_vec.load(c)
        c.enable_load_extension(False)
        yield c
    finally:
        c.close()


def _snapshot_schema(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Return a sorted list of all rows in ``sqlite_master`` — used to
    verify the probe leaves zero residue.
    """
    rows = conn.execute(
        "SELECT type, name, COALESCE(sql, '') FROM sqlite_master ORDER BY name"
    ).fetchall()
    return rows


# ---------------------------------------------------------------------------
# Positive path — every mainstream Python build hits this case
# ---------------------------------------------------------------------------


def test_returns_dataclass_with_three_bool_fields(conn: sqlite3.Connection) -> None:
    """The return type is a frozen ``DbFeatures`` carrying the AC-listed flags."""
    features = get_lcm_db_features(conn)
    assert isinstance(features, DbFeatures)
    assert isinstance(features.fts5_available, bool)
    assert isinstance(features.fts5_trigram_available, bool)
    assert isinstance(features.vec0_available, bool)


def test_fts5_available_on_stdlib_sqlite3(conn: sqlite3.Connection) -> None:
    """Per spike 005: every CPython 3.11+ stdlib ``sqlite3`` has FTS5.

    Failing this test would indicate a custom-compiled Python — file a
    bug and fall through to the graceful-degrade path on the migration.
    """
    features = get_lcm_db_features(conn)
    assert features.fts5_available is True


def test_trigram_available_on_stdlib_sqlite3(conn: sqlite3.Connection) -> None:
    """Per spike 005: trigram tokenizer is registered on every CPython
    3.11+ stdlib (SQLite 3.34+, Python 3.11 ships 3.39+).
    """
    features = get_lcm_db_features(conn)
    assert features.fts5_trigram_available is True


def test_vec0_available_with_sqlite_vec_loaded(
    conn_with_vec0: sqlite3.Connection,
) -> None:
    """After ``sqlite_vec.load(conn)``, the vec0 probe must succeed.

    Mirrors what ``db.connection.open_lcm_db`` (issue 01-01) will do at
    connection-open time.
    """
    features = get_lcm_db_features(conn_with_vec0)
    assert features.vec0_available is True


def test_vec0_unavailable_without_extension_load(conn: sqlite3.Connection) -> None:
    """Without ``sqlite_vec.load(conn)``, vec0 must report unavailable.

    This is the graceful-degrade contract: if the host Python build can't
    load sqlite-vec, the embedding path is disabled at the store layer
    instead of crashing.
    """
    features = get_lcm_db_features(conn)
    assert features.vec0_available is False


def test_full_positive_path_when_extensions_loaded(
    conn_with_vec0: sqlite3.Connection,
) -> None:
    """All three flags are ``True`` on a Homebrew Python with sqlite-vec loaded —
    this is the canonical production configuration per ADR-024.
    """
    features = get_lcm_db_features(conn_with_vec0)
    assert features == DbFeatures(
        fts5_available=True,
        fts5_trigram_available=True,
        vec0_available=True,
    )


# ---------------------------------------------------------------------------
# Schema residue — the probe must leave sqlite_master byte-identical
# ---------------------------------------------------------------------------


def test_probe_leaves_no_schema_residue(conn: sqlite3.Connection) -> None:
    """``sqlite_master`` is byte-identical before and after the probe.

    Verifies the SAVEPOINT/ROLLBACK pattern correctly discards the
    probe virtual tables. Also exercises a fresh-connection probe
    (cache cold) so we hit every CREATE path.
    """
    before = _snapshot_schema(conn)
    get_lcm_db_features(conn)
    after = _snapshot_schema(conn)
    assert before == after, f"probe left residue in sqlite_master:\nbefore={before}\nafter={after}"


def test_probe_leaves_no_schema_residue_with_vec0_loaded(
    conn_with_vec0: sqlite3.Connection,
) -> None:
    """Same residue invariant on a connection with sqlite-vec loaded —
    exercises the vec0 probe's CREATE path too.
    """
    before = _snapshot_schema(conn_with_vec0)
    get_lcm_db_features(conn_with_vec0)
    after = _snapshot_schema(conn_with_vec0)
    assert before == after


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_repeated_call_on_same_conn_returns_cached_object(
    conn: sqlite3.Connection,
) -> None:
    """First call probes; second call returns the **same** dataclass
    instance (Python ``is``, not just ``==``). Proves the cache short-
    circuits the probe — counted as an explicit AC item.
    """
    first = get_lcm_db_features(conn)
    second = get_lcm_db_features(conn)
    assert first is second


def test_different_conn_does_not_share_cache_entry() -> None:
    """Each ``Connection`` object gets its own probe + cache entry.

    The flags happen to come out equal (same Python build), but they
    must NOT be the same object — that would mean the cache key was
    something other than the ``Connection`` instance, which would
    silently break vec0 detection when one connection has loaded
    sqlite-vec and another hasn't.
    """
    conn_a = sqlite3.connect(":memory:")
    conn_b = sqlite3.connect(":memory:")
    try:
        feat_a = get_lcm_db_features(conn_a)
        feat_b = get_lcm_db_features(conn_b)
        assert feat_a == feat_b  # same Python build ⇒ same flags
        assert feat_a is not feat_b  # but different objects
    finally:
        conn_a.close()
        conn_b.close()


class _CountingConnection(sqlite3.Connection):
    """``sqlite3.Connection`` subclass that counts ``execute()`` calls.

    The stdlib ``sqlite3.Connection`` makes ``execute`` a read-only
    attribute (``AttributeError: ... attribute 'execute' is read-only``)
    so ``monkeypatch.setattr(conn, "execute", ...)`` cannot wrap it.
    Subclassing and overriding the method is the supported path —
    ``sqlite3.connect(..., factory=_CountingConnection)`` returns an
    instance of this subclass, and the override is honored.
    """

    execute_call_count: int = 0

    def execute(self, sql: str, *args: object, **kwargs: object) -> sqlite3.Cursor:  # type: ignore[override]
        self.execute_call_count += 1
        return super().execute(sql, *args, **kwargs)


def test_cache_does_not_reprobe_on_repeated_calls() -> None:
    """A counting subclass counts ``Connection.execute`` calls; the
    second ``get_lcm_db_features`` call must NOT increment the counter.

    This is the AC item "subsequent calls return the cached value
    without re-probing (verified via pytest-level call counter)" from
    the issue spec, item 2. Implementation note: stdlib
    ``sqlite3.Connection.execute`` is a read-only attribute, so we use a
    ``factory=`` subclass to count calls rather than ``monkeypatch``.
    """
    conn_counter = sqlite3.connect(":memory:", factory=_CountingConnection)
    try:
        first = get_lcm_db_features(conn_counter)
        before = conn_counter.execute_call_count
        assert before > 0, "first call must run probes"

        second = get_lcm_db_features(conn_counter)
        after = conn_counter.execute_call_count

        assert second is first, "cached call must return same DbFeatures instance"
        assert after == before, (
            f"cache miss: execute() called {after - before} times after cache prime"
        )
    finally:
        conn_counter.close()


def test_clear_db_features_cache_for_one_conn() -> None:
    """``clear_db_features_cache(conn)`` drops the entry for that
    connection only — other cached entries are untouched.

    This is the explicit-cleanup escape hatch documented in the module
    header: callers that want strict cache hygiene can call it from a
    teardown fixture. The TS ``WeakKeyDictionary``-eviction AC item is
    not implementable for stdlib ``sqlite3.Connection`` (no
    ``__weakref__`` slot — see module docstring §"Cache"); this test
    exercises the equivalent contract via explicit invalidation.
    """
    conn_a = sqlite3.connect(":memory:")
    conn_b = sqlite3.connect(":memory:")
    try:
        feat_a = get_lcm_db_features(conn_a)
        feat_b = get_lcm_db_features(conn_b)
        assert id(conn_a) in _feature_cache
        assert id(conn_b) in _feature_cache

        clear_db_features_cache(conn_a)
        assert id(conn_a) not in _feature_cache, "targeted entry should be gone"
        assert id(conn_b) in _feature_cache, "untargeted entry must remain"

        # And a fresh probe rebuilds it.
        feat_a_again = get_lcm_db_features(conn_a)
        assert feat_a_again == feat_a  # same flags
    finally:
        conn_a.close()
        conn_b.close()


def test_clear_db_features_cache_all() -> None:
    """``clear_db_features_cache()`` (no arg) empties the entire cache."""
    conn_a = sqlite3.connect(":memory:")
    conn_b = sqlite3.connect(":memory:")
    try:
        get_lcm_db_features(conn_a)
        get_lcm_db_features(conn_b)
        assert len(_feature_cache) >= 2

        clear_db_features_cache()
        assert len(_feature_cache) == 0
    finally:
        conn_a.close()
        conn_b.close()


# ---------------------------------------------------------------------------
# Negative path — explicit AC item from the issue spec
# ---------------------------------------------------------------------------


class _TrigramFailingConnection(sqlite3.Connection):
    """Subclass that fails any ``execute`` mentioning trigram tokenizer.

    Simulates a SQLite build with FTS5 but without the trigram
    tokenizer registered — the exact failure mode the
    ``fts5_trigram_available`` flag protects against (per spike 005
    §"Remaining 5% risk" item 3 — custom Python builds with FTS5 only).
    """

    def execute(self, sql: str, *args: object, **kwargs: object) -> sqlite3.Cursor:  # type: ignore[override]
        if "tokenize='trigram'" in sql:
            raise sqlite3.OperationalError("no such tokenizer: trigram")
        return super().execute(sql, *args, **kwargs)


class _Fts5FailingConnection(sqlite3.Connection):
    """Subclass that fails any ``execute`` mentioning ``fts5``.

    Simulates a custom-compiled Python with FTS5 entirely disabled
    (per `docs/porting-guides/storage.md` §12 risk #4).
    """

    def execute(self, sql: str, *args: object, **kwargs: object) -> sqlite3.Cursor:  # type: ignore[override]
        if "fts5" in sql.lower():
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, *args, **kwargs)


def test_trigram_probe_failure_yields_false_flag() -> None:
    """A connection whose ``execute`` raises on trigram-tokenize
    statements must yield ``fts5_trigram_available is False`` while
    ``fts5_available`` stays ``True`` (the trigram failure must not
    contaminate the FTS5 answer).

    This is the AC item "Negative test: mock ``conn.execute`` to raise
    ``OperationalError('no such tokenizer: trigram')`` on the trigram
    probe" from the issue spec.
    """
    conn_trigfail = sqlite3.connect(":memory:", factory=_TrigramFailingConnection)
    try:
        features = get_lcm_db_features(conn_trigfail)
        assert features.fts5_available is True
        assert features.fts5_trigram_available is False
    finally:
        conn_trigfail.close()


def test_fts5_probe_failure_yields_false_for_both_fts_flags() -> None:
    """When FTS5 itself is unavailable, the trigram probe must be
    short-circuited to ``False`` (matches TS contract
    ``src/db/features.ts:53-58``).
    """
    conn_fts5fail = sqlite3.connect(":memory:", factory=_Fts5FailingConnection)
    try:
        features = get_lcm_db_features(conn_fts5fail)
        assert features.fts5_available is False
        assert features.fts5_trigram_available is False
    finally:
        conn_fts5fail.close()


def test_vec0_probe_failure_yields_false_flag(conn: sqlite3.Connection) -> None:
    """Without ``sqlite_vec.load(conn)``, the vec0 probe raises and the
    flag is ``False``. Already covered by
    ``test_vec0_unavailable_without_extension_load`` but keep this case
    explicit so the negative-path table is complete (FTS5 / trigram /
    vec0 all have a failure-mode test).
    """
    features = get_lcm_db_features(conn)
    assert features.vec0_available is False


# ---------------------------------------------------------------------------
# Immutability — guards against accidental tampering downstream
# ---------------------------------------------------------------------------


def test_db_features_is_frozen(conn: sqlite3.Connection) -> None:
    """The dataclass is ``frozen=True`` — downstream stores cannot mutate
    the flags out from under each other. Raises
    :class:`dataclasses.FrozenInstanceError`.
    """
    import dataclasses

    features = get_lcm_db_features(conn)
    with pytest.raises(dataclasses.FrozenInstanceError):
        features.fts5_available = False  # type: ignore[misc]
