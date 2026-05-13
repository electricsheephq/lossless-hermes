"""Tests for :mod:`lossless_hermes.embeddings.store` (issue 05-03).

Ports ``lossless-claw/test/embeddings-store.test.ts`` (525 LOC) to Python.

vec0-dependent tests are gated on the extension being loadable on this
Python build. ``actions/setup-python`` on macOS sometimes ships a CPython
without ``--enable-loadable-sqlite-extensions`` — on those runners every
vec0 test skips cleanly. On Homebrew Python (spike-001 PASS) and the
Linux runners, the full vec0 suite runs.

The TS suite uses ``LCM_TEST_VEC0_PATH`` env var to point at a bundled
``vec0.<dylib|so|dll>``. In Python the PyPI ``sqlite_vec`` wheel ships
the extension binary itself — no env var needed; we just try the load
and skip the suite if it fails.

Test isolation: each test opens its own ``:memory:`` connection via
:func:`_open_test_conn`, runs migrations (which create the
``lcm_embedding_meta`` + ``lcm_embedding_profile`` tables), then exercises
the store API. The connection is closed by the ``finally`` clause.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.embeddings.store import (
    EmbeddedKind,
    MAX_EMBEDDING_DIM,
    SearchHit,
    delete_embedding,
    drop_embeddings_triggers,
    embeddings_table_exists,
    embeddings_table_name,
    ensure_embeddings_table,
    is_embedded,
    mark_embedding_suppressed,
    record_embedding,
    register_embedding_profile,
    replace_embedding,
    search_similar,
)


# ---------------------------------------------------------------------------
# vec0 availability probe — gates the vec0-dependent suite
# ---------------------------------------------------------------------------


def _vec0_loadable() -> bool:
    """Return :data:`True` iff ``sqlite_vec.load`` succeeds on this Python.

    Mirrors the TS suite's ``VEC0_AVAILABLE`` guard. We probe at module
    import time so the :func:`pytest.mark.skipif` decorator has a value
    to consume before any test runs.
    """
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        return False
    try:
        import sqlite_vec  # local import to keep top-level cheap

        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            return True
        finally:
            conn.close()
    except (AttributeError, sqlite3.OperationalError):
        return False


VEC0_AVAILABLE: bool = _vec0_loadable()


skip_if_no_vec0 = pytest.mark.skipif(
    not VEC0_AVAILABLE,
    reason=(
        "sqlite-vec extension not loadable on this Python build "
        "(actions/setup-python on macOS often disables loadable "
        "extensions). Vec0-dependent tests skip cleanly."
    ),
)


# ---------------------------------------------------------------------------
# Test fixtures (connection helpers)
# ---------------------------------------------------------------------------


def _open_test_conn(*, load_vec0: bool = False) -> sqlite3.Connection:
    """Open a bare ``:memory:`` connection. Loads sqlite-vec if requested.

    Tests that don't need vec0 SQL pass ``load_vec0=False`` (default). The
    vec0-gated suite passes ``load_vec0=True`` and is wrapped by
    :data:`skip_if_no_vec0` so the load is guaranteed to succeed.
    """
    conn = sqlite3.connect(":memory:")
    if load_vec0:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


@pytest.fixture
def conn_no_vec0() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with the v4.1 migration ladder run but NO vec0.

    Used for tests that exercise non-vec0 surfaces — slug rules, profile
    registration, ``is_embedded`` (meta-only), and the early ``register*``
    validation guards.
    """
    conn = _open_test_conn(load_vec0=False)
    try:
        run_lcm_migrations(conn, fts5_available=False)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def conn_with_vec0() -> Iterator[sqlite3.Connection]:
    """In-memory SQLite with vec0 + the v4.1 migration ladder applied.

    Used for tests that issue vec0 SQL (``ensure_embeddings_table``,
    ``record_embedding``, ``search_similar``, etc.). The fixture is
    guarded by :data:`skip_if_no_vec0` at the test level — pytest will
    skip cleanly if the extension can't load.
    """
    conn = _open_test_conn(load_vec0=True)
    try:
        run_lcm_migrations(conn, fts5_available=False)
        yield conn
    finally:
        conn.close()


# ===========================================================================
# Non-vec0 tests (always run)
# ===========================================================================


class TestEmbeddingsTableName:
    """``embeddings_table_name`` — slug normalization rules."""

    def test_sluggifies_voyage_4_large(self) -> None:
        assert embeddings_table_name("voyage-4-large") == "lcm_embeddings_voyage4large"

    def test_sluggifies_voyage_3_lite(self) -> None:
        assert embeddings_table_name("voyage-3-lite") == "lcm_embeddings_voyage3lite"

    def test_rejects_empty_model_name(self) -> None:
        with pytest.raises(ValueError, match="invalid model name"):
            embeddings_table_name("")

    def test_rejects_sql_injection_attempt(self) -> None:
        with pytest.raises(ValueError, match="invalid model name"):
            embeddings_table_name("foo; DROP TABLE")

    def test_rejects_quote_in_name(self) -> None:
        with pytest.raises(ValueError, match="invalid model name"):
            embeddings_table_name("foo'bar")

    def test_rejects_newline_in_name(self) -> None:
        with pytest.raises(ValueError, match="invalid model name"):
            embeddings_table_name("foo\nbar")

    def test_rejects_overlong_name(self) -> None:
        # 64-char name OK; 65-char name rejected.
        sixty_four = "a" * 64
        assert embeddings_table_name(sixty_four) == f"lcm_embeddings_{sixty_four}"
        with pytest.raises(ValueError, match="invalid model name"):
            embeddings_table_name("a" * 65)

    def test_rejects_slug_that_normalizes_to_empty(self) -> None:
        with pytest.raises(ValueError, match="sluggifies to empty"):
            embeddings_table_name("___")
        with pytest.raises(ValueError, match="sluggifies to empty"):
            embeddings_table_name("...")
        with pytest.raises(ValueError, match="sluggifies to empty"):
            embeddings_table_name("-.-")

    def test_accepts_dot_underscore_dash_characters(self) -> None:
        assert embeddings_table_name("voyage.4_large-test") == "lcm_embeddings_voyage4largetest"


class TestRegisterEmbeddingProfile:
    """``register_embedding_profile`` — happy path + validation."""

    def test_inserts_profile_row_idempotent_on_same_dim(
        self, conn_no_vec0: sqlite3.Connection
    ) -> None:
        register_embedding_profile(conn_no_vec0, "voyage-4-large", 1024)
        row = conn_no_vec0.execute(
            "SELECT model_name, dim, active FROM lcm_embedding_profile WHERE model_name = ?",
            ("voyage-4-large",),
        ).fetchone()
        assert row == ("voyage-4-large", 1024, 1)

        # Second call with same dim is a no-op — only one row exists.
        register_embedding_profile(conn_no_vec0, "voyage-4-large", 1024)
        count = conn_no_vec0.execute(
            "SELECT COUNT(*) FROM lcm_embedding_profile WHERE model_name = ?",
            ("voyage-4-large",),
        ).fetchone()[0]
        assert count == 1

    def test_throws_on_dim_mismatch_for_existing_profile(
        self, conn_no_vec0: sqlite3.Connection
    ) -> None:
        register_embedding_profile(conn_no_vec0, "voyage-4-large", 1024)
        with pytest.raises(ValueError, match="dim mismatch"):
            register_embedding_profile(conn_no_vec0, "voyage-4-large", 2048)

    def test_rejects_bad_model_name(self, conn_no_vec0: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid model name"):
            register_embedding_profile(conn_no_vec0, "foo;DROP", 1024)

    def test_rejects_zero_dim(self, conn_no_vec0: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid dim"):
            register_embedding_profile(conn_no_vec0, "voyage-x", 0)

    def test_rejects_negative_dim(self, conn_no_vec0: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid dim"):
            register_embedding_profile(conn_no_vec0, "voyage-x", -5)

    def test_rejects_overlarge_dim(self, conn_no_vec0: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid dim"):
            register_embedding_profile(conn_no_vec0, "voyage-x", MAX_EMBEDDING_DIM + 1)

    def test_rejects_bool_dim(self, conn_no_vec0: sqlite3.Connection) -> None:
        # ``bool`` is a subclass of ``int`` — guard explicitly so
        # ``register_embedding_profile(db, "x", True)`` doesn't silently
        # create a dim-1 profile.
        with pytest.raises(ValueError, match="invalid dim"):
            register_embedding_profile(conn_no_vec0, "voyage-x", True)  # type: ignore[arg-type]

    def test_slug_collision_guard_blocks_second_model(
        self, conn_no_vec0: sqlite3.Connection
    ) -> None:
        # "voyage-4-large" and "voyage_4_large" both sluggify to
        # "voyage4large" — the second registration must throw.
        register_embedding_profile(conn_no_vec0, "voyage-4-large", 1024)
        with pytest.raises(ValueError, match="slug collision"):
            register_embedding_profile(conn_no_vec0, "voyage_4_large", 1024)

    def test_slug_collision_guard_blocks_three_way_collision(
        self, conn_no_vec0: sqlite3.Connection
    ) -> None:
        # Defense in depth — also catches dots/dashes interleaved with
        # underscores all collapsing to the same slug.
        register_embedding_profile(conn_no_vec0, "voyage-4-large", 1024)
        with pytest.raises(ValueError, match="slug collision"):
            register_embedding_profile(conn_no_vec0, "voyage.4.large", 1024)


class TestIsEmbeddedMetaOnly:
    """``is_embedded`` — pure ``lcm_embedding_meta`` lookup; no vec0 touch."""

    def test_returns_false_when_no_meta_row_exists(self, conn_no_vec0: sqlite3.Connection) -> None:
        # No profile registered, no meta row — should return False.
        assert (
            is_embedded(
                conn_no_vec0,
                embedded_id="leaf_a",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is False
        )

    def test_invalid_kind_raises(self, conn_no_vec0: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid embedded_kind"):
            is_embedded(
                conn_no_vec0,
                embedded_id="x",
                embedded_kind="not-a-kind",  # type: ignore[arg-type]
                model_name="voyage-4-large",
            )


class TestEmbeddingsTableExistsNoVec0:
    """``embeddings_table_exists`` — sqlite_master check; safe without vec0."""

    def test_returns_false_when_table_absent(self, conn_no_vec0: sqlite3.Connection) -> None:
        assert embeddings_table_exists(conn_no_vec0, "voyage-4-large") is False

    def test_returns_false_for_other_models(self, conn_no_vec0: sqlite3.Connection) -> None:
        # Without vec0, no table can exist — every model returns False.
        assert embeddings_table_exists(conn_no_vec0, "voyage-3-lite") is False


# ===========================================================================
# vec0-dependent tests — skip cleanly when extension can't load
# ===========================================================================


@skip_if_no_vec0
class TestVec0Load:
    """Sanity: vec0 loads and ``vec_version()`` returns a version string."""

    def test_vec0_version_present(self) -> None:
        # We use ``vec0_version`` from the connection module rather than the
        # store module — store.py doesn't re-export it (it lives in
        # ``connection.py`` per issue 05-04). The test verifies that
        # ``conn_with_vec0`` actually loaded the extension successfully.
        from lossless_hermes.db.connection import vec0_version

        conn = _open_test_conn(load_vec0=True)
        try:
            version = vec0_version(conn)
            assert version is not None
            assert version.startswith("v")
        finally:
            conn.close()


@skip_if_no_vec0
class TestEnsureEmbeddingsTable:
    """``ensure_embeddings_table`` — DDL creation + idempotency."""

    def test_creates_vec0_virtual_table_and_is_idempotent(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 1024)

        # Table doesn't exist initially.
        assert embeddings_table_exists(conn_with_vec0, "voyage-4-large") is False

        # First call creates it.
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 1024)
        assert embeddings_table_exists(conn_with_vec0, "voyage-4-large") is True

        # Idempotent — second call is a no-op.
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 1024)
        assert embeddings_table_exists(conn_with_vec0, "voyage-4-large") is True

    def test_creates_per_model_triggers(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 1024)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 1024)

        # Both triggers must exist after ``ensure_embeddings_table``.
        triggers = {
            row[0]
            for row in conn_with_vec0.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert "lcm_embed_suppress_voyage4large" in triggers
        assert "lcm_embed_delete_voyage4large" in triggers

    def test_rejects_zero_dim(self, conn_with_vec0: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid dim"):
            ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 0)

    def test_rejects_overlarge_dim(self, conn_with_vec0: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid dim"):
            ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 99999)


@skip_if_no_vec0
class TestDropEmbeddingsTriggers:
    """``drop_embeddings_triggers`` — model archival path."""

    def test_drops_both_triggers(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-3-lite", 512)
        ensure_embeddings_table(conn_with_vec0, "voyage-3-lite", 512)

        drop_embeddings_triggers(conn_with_vec0, "voyage-3-lite")

        triggers = {
            row[0]
            for row in conn_with_vec0.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert "lcm_embed_suppress_voyage3lite" not in triggers
        assert "lcm_embed_delete_voyage3lite" not in triggers
        # Table preserved by default.
        assert embeddings_table_exists(conn_with_vec0, "voyage-3-lite") is True

    def test_drops_table_when_drop_table_true(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-3-lite", 512)
        ensure_embeddings_table(conn_with_vec0, "voyage-3-lite", 512)

        drop_embeddings_triggers(conn_with_vec0, "voyage-3-lite", drop_table=True)
        assert embeddings_table_exists(conn_with_vec0, "voyage-3-lite") is False


@skip_if_no_vec0
class TestRecordEmbedding:
    """``record_embedding`` — vec0 + meta atomic write."""

    def test_inserts_vec0_row_and_meta_row(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 4)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 4)

        # Before: not embedded.
        assert (
            is_embedded(
                conn_with_vec0,
                embedded_id="leaf_a",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is False
        )

        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_token_count=100,
        )

        # After: embedded + meta row mirrors source_token_count.
        assert (
            is_embedded(
                conn_with_vec0,
                embedded_id="leaf_a",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is True
        )
        meta = conn_with_vec0.execute(
            "SELECT source_token_count, archived FROM lcm_embedding_meta WHERE embedded_id = ?",
            ("leaf_a",),
        ).fetchone()
        assert meta == (100, 0)

    def test_rejects_wrong_dimension_vector(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 4)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 4)
        with pytest.raises(ValueError, match="dim mismatch"):
            record_embedding(
                conn_with_vec0,
                model_name="voyage-4-large",
                embedded_id="leaf_a",
                embedded_kind="summary",
                vector=[0.1, 0.2],  # dim 2, not 4
                source_token_count=100,
            )

    def test_rejects_when_no_profile_registered(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Skip register_embedding_profile — should fail at the meta lookup.
        with pytest.raises(ValueError, match="no profile registered"):
            record_embedding(
                conn_with_vec0,
                model_name="voyage-4-large",
                embedded_id="x",
                embedded_kind="summary",
                vector=[0.1],
                source_token_count=1,
            )

    def test_rejects_bad_kind(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)
        with pytest.raises(ValueError, match="invalid embedded_kind"):
            record_embedding(
                conn_with_vec0,
                model_name="voyage-4-large",
                embedded_id="x",
                embedded_kind="not-a-kind",  # type: ignore[arg-type]
                vector=[0.1, 0.2, 0.3],
                source_token_count=1,
            )

    def test_back_to_back_record_does_not_duplicate_vec0_row(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Wave-4 fix verification: DELETE-before-INSERT ensures no
        # duplicate vec0 rows for the same (embedded_id, embedded_kind).
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)

        for _ in range(3):
            record_embedding(
                conn_with_vec0,
                model_name="voyage-4-large",
                embedded_id="leaf_a",
                embedded_kind="summary",
                vector=[0.1, 0.2, 0.3],
                source_token_count=1,
            )

        table = embeddings_table_name("voyage-4-large")
        count = conn_with_vec0.execute(
            f"SELECT COUNT(*) FROM {table} WHERE embedded_id = ?",
            ("leaf_a",),
        ).fetchone()[0]
        assert count == 1

    def test_accepts_pre_serialized_bytes_vector(self, conn_with_vec0: sqlite3.Connection) -> None:
        # The bytes path is the fast path per spike 001. Verify callers
        # that pre-serialize their vectors (e.g., backfill bulk path) work.
        import sqlite_vec

        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)
        vec_bytes = sqlite_vec.serialize_float32([0.1, 0.2, 0.3])
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_bytes",
            embedded_kind="summary",
            vector=vec_bytes,
            source_token_count=1,
        )
        assert (
            is_embedded(
                conn_with_vec0,
                embedded_id="leaf_bytes",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is True
        )

    def test_rejects_pre_serialized_bytes_of_wrong_length(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # Defense-in-depth: even pre-serialized bytes must match dim.
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)
        wrong_size_bytes = b"\x00" * 8  # 8 bytes = 2 floats, not 3
        with pytest.raises(ValueError, match="bytes, expected"):
            record_embedding(
                conn_with_vec0,
                model_name="voyage-4-large",
                embedded_id="x",
                embedded_kind="summary",
                vector=wrong_size_bytes,
                source_token_count=1,
            )


@skip_if_no_vec0
class TestSearchSimilar:
    """``search_similar`` — KNN with metadata filters."""

    def test_finds_nearest_excludes_suppressed_by_default(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)

        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],
            source_token_count=1,
        )
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_b",
            embedded_kind="summary",
            vector=[0.4, 0.5, 0.6],
            source_token_count=1,
        )
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_suppressed",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],  # identical to leaf_a, but suppressed
            suppressed=True,
            source_token_count=1,
        )

        hits = search_similar(
            conn_with_vec0,
            model_name="voyage-4-large",
            query_vector=[0.1, 0.2, 0.3],
            k=5,
        )
        assert len(hits) == 2
        assert hits[0].embedded_id == "leaf_a"
        ids = {h.embedded_id for h in hits}
        assert "leaf_suppressed" not in ids

    def test_includes_suppressed_when_exclude_false(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)

        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_visible",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],
            source_token_count=1,
        )
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_hidden",
            embedded_kind="summary",
            vector=[0.4, 0.5, 0.6],
            suppressed=True,
            source_token_count=1,
        )

        hits = search_similar(
            conn_with_vec0,
            model_name="voyage-4-large",
            query_vector=[0.1, 0.2, 0.3],
            k=5,
            exclude_suppressed=False,
        )
        assert len(hits) == 2
        assert {h.embedded_id for h in hits} == {"leaf_visible", "leaf_hidden"}

    def test_filters_by_embedded_kind(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)

        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            vector=[0.1, 0.1, 0.1],
            source_token_count=1,
        )
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="ent_b",
            embedded_kind="entity",
            vector=[0.1, 0.1, 0.1],
            source_token_count=1,
        )

        # Default kinds=("summary",) — only the summary row appears.
        only_summaries = search_similar(
            conn_with_vec0,
            model_name="voyage-4-large",
            query_vector=[0.1, 0.1, 0.1],
            k=5,
        )
        assert [h.embedded_id for h in only_summaries] == ["leaf_a"]

        only_entities = search_similar(
            conn_with_vec0,
            model_name="voyage-4-large",
            query_vector=[0.1, 0.1, 0.1],
            k=5,
            embedded_kinds=["entity"],
        )
        assert [h.embedded_id for h in only_entities] == ["ent_b"]

        both = search_similar(
            conn_with_vec0,
            model_name="voyage-4-large",
            query_vector=[0.1, 0.1, 0.1],
            k=5,
            embedded_kinds=["summary", "entity"],
        )
        assert {h.embedded_id for h in both} == {"leaf_a", "ent_b"}

    def test_empty_kinds_returns_empty_list(self, conn_with_vec0: sqlite3.Connection) -> None:
        # Defense-in-depth: an empty embedded_kinds is a no-op short-circuit
        # (matches ``store.ts:552`` ``if (kinds.length === 0) return []``).
        # No table required since we never issue the SQL.
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)
        assert (
            search_similar(
                conn_with_vec0,
                model_name="voyage-4-large",
                query_vector=[0.1, 0.1, 0.1],
                k=5,
                embedded_kinds=[],
            )
            == []
        )

    def test_rejects_invalid_k(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)
        with pytest.raises(ValueError, match="invalid k"):
            search_similar(
                conn_with_vec0,
                model_name="voyage-4-large",
                query_vector=[0.1, 0.2, 0.3],
                k=0,
            )
        with pytest.raises(ValueError, match="invalid k"):
            search_similar(
                conn_with_vec0,
                model_name="voyage-4-large",
                query_vector=[0.1, 0.2, 0.3],
                k=99999,
            )

    def test_returns_search_hit_dataclass(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],
            source_token_count=1,
        )
        hits = search_similar(
            conn_with_vec0,
            model_name="voyage-4-large",
            query_vector=[0.1, 0.2, 0.3],
            k=1,
        )
        assert len(hits) == 1
        assert isinstance(hits[0], SearchHit)
        # L2 distance to self should be ~0.
        assert hits[0].distance < 1e-5


@skip_if_no_vec0
class TestMarkEmbeddingSuppressed:
    """``mark_embedding_suppressed`` — metadata-column UPDATE (safe for vec0)."""

    def test_flips_visibility_in_subsequent_search(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],
            source_token_count=1,
        )

        # Before: visible.
        before = search_similar(
            conn_with_vec0,
            model_name="voyage-4-large",
            query_vector=[0.1, 0.2, 0.3],
            k=5,
        )
        assert [h.embedded_id for h in before] == ["leaf_a"]

        # Suppress.
        mark_embedding_suppressed(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            suppressed=True,
        )

        # After: filtered out by the metadata pre-filter.
        after = search_similar(
            conn_with_vec0,
            model_name="voyage-4-large",
            query_vector=[0.1, 0.2, 0.3],
            k=5,
        )
        assert after == []

        # Restore.
        mark_embedding_suppressed(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            suppressed=False,
        )
        restored = search_similar(
            conn_with_vec0,
            model_name="voyage-4-large",
            query_vector=[0.1, 0.2, 0.3],
            k=5,
        )
        assert [h.embedded_id for h in restored] == ["leaf_a"]


@skip_if_no_vec0
class TestReplaceEmbedding:
    """``replace_embedding`` — DELETE + INSERT atomically."""

    def test_removes_prior_and_inserts_new(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)

        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            vector=[0.1, 0.0, 0.0],
            source_token_count=100,
        )
        replace_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            vector=[0.0, 0.0, 1.0],  # very different
            source_token_count=200,
        )

        table = embeddings_table_name("voyage-4-large")
        count = conn_with_vec0.execute(
            f"SELECT COUNT(*) FROM {table} WHERE embedded_id = ?",
            ("leaf_a",),
        ).fetchone()[0]
        # Not 2 — old row removed.
        assert count == 1

        meta = conn_with_vec0.execute(
            "SELECT source_token_count FROM lcm_embedding_meta WHERE embedded_id = ?",
            ("leaf_a",),
        ).fetchone()
        assert meta == (200,)


@skip_if_no_vec0
class TestDeleteEmbedding:
    """``delete_embedding`` — removes from both vec0 and meta."""

    def test_removes_from_both_tables(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],
            source_token_count=1,
        )
        delete_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
        )

        assert (
            is_embedded(
                conn_with_vec0,
                embedded_id="leaf_a",
                embedded_kind="summary",
                model_name="voyage-4-large",
            )
            is False
        )
        table = embeddings_table_name("voyage-4-large")
        count = conn_with_vec0.execute(
            f"SELECT COUNT(*) FROM {table} WHERE embedded_id = ?",
            ("leaf_a",),
        ).fetchone()[0]
        assert count == 0


@skip_if_no_vec0
class TestMultipleModels:
    """Two distinct models live in two distinct vec0 tables."""

    def test_two_models_get_two_independent_tables(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 4)
        register_embedding_profile(conn_with_vec0, "voyage-3-lite", 2)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 4)
        ensure_embeddings_table(conn_with_vec0, "voyage-3-lite", 2)

        assert embeddings_table_exists(conn_with_vec0, "voyage-4-large") is True
        assert embeddings_table_exists(conn_with_vec0, "voyage-3-lite") is True
        assert embeddings_table_name("voyage-4-large") != embeddings_table_name("voyage-3-lite")

        # Data isolation: an insert into model A doesn't appear in model B.
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="leaf_a",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3, 0.4],
            source_token_count=1,
        )
        # ``is_embedded`` for model B returns False even with the same id.
        assert (
            is_embedded(
                conn_with_vec0,
                embedded_id="leaf_a",
                embedded_kind="summary",
                model_name="voyage-3-lite",
            )
            is False
        )


@skip_if_no_vec0
class TestPolymorphicEmbeddedKind:
    """Polymorphic: summaries + entities + themes share one model's table."""

    def test_three_kinds_coexist_in_one_table(self, conn_with_vec0: sqlite3.Connection) -> None:
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)

        for kind, ident in (
            ("summary", "s_1"),
            ("entity", "e_1"),
            ("theme", "t_1"),
        ):
            record_embedding(
                conn_with_vec0,
                model_name="voyage-4-large",
                embedded_id=ident,
                embedded_kind=kind,  # type: ignore[arg-type]
                vector=[0.1, 0.1, 0.1],
                source_token_count=1,
            )

        table = embeddings_table_name("voyage-4-large")
        # Three distinct rows in one vec0 table.
        rows = conn_with_vec0.execute(
            f"SELECT embedded_id, embedded_kind FROM {table} ORDER BY embedded_id"
        ).fetchall()
        assert sorted(rows) == sorted([("e_1", "entity"), ("s_1", "summary"), ("t_1", "theme")])


# ===========================================================================
# Trigger cascade tests — AFTER UPDATE + AFTER DELETE on summaries
# ===========================================================================


@skip_if_no_vec0
class TestAfterUpdateTrigger:
    """AFTER UPDATE OF suppressed_at ON summaries cascades to vec0."""

    def _seed_summary_and_embedding(
        self, conn: sqlite3.Connection, model: str, summary_id: str
    ) -> None:
        """Insert a summary row and its embedding so the trigger has work."""
        # The ``summaries`` table has multiple NOT NULL columns and an FK to
        # ``conversations`` (conversation_id INTEGER, AUTOINCREMENT). Seed
        # the parent row first so the FK is satisfied — without it the
        # INSERT raises IntegrityError before the trigger ever fires.
        conn.execute(
            "INSERT OR IGNORE INTO conversations (conversation_id, session_id) "
            "VALUES (1, 'session-test')"
        )
        conn.execute(
            "INSERT INTO summaries "
            "  (summary_id, conversation_id, kind, depth, content, "
            "   token_count, created_at) "
            "VALUES (?, 1, 'leaf', 0, 'content', 10, datetime('now'))",
            (summary_id,),
        )
        register_embedding_profile(conn, model, 3)
        ensure_embeddings_table(conn, model, 3)
        record_embedding(
            conn,
            model_name=model,
            embedded_id=summary_id,
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],
            source_token_count=1,
        )

    def test_setting_suppressed_at_cascades_to_vec0(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        self._seed_summary_and_embedding(conn_with_vec0, "voyage-4-large", "summary_z")
        table = embeddings_table_name("voyage-4-large")

        # Before: suppressed=0 in vec0.
        before = conn_with_vec0.execute(
            f"SELECT suppressed FROM {table} WHERE embedded_id = ?",
            ("summary_z",),
        ).fetchone()[0]
        assert before == 0

        # UPDATE suppressed_at — the AFTER UPDATE trigger should fire.
        conn_with_vec0.execute(
            "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
            ("summary_z",),
        )

        after = conn_with_vec0.execute(
            f"SELECT suppressed FROM {table} WHERE embedded_id = ?",
            ("summary_z",),
        ).fetchone()[0]
        assert after == 1

    def test_clearing_suppressed_at_restores_visibility(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        self._seed_summary_and_embedding(conn_with_vec0, "voyage-4-large", "summary_z")
        table = embeddings_table_name("voyage-4-large")

        # Set then clear.
        conn_with_vec0.execute(
            "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
            ("summary_z",),
        )
        conn_with_vec0.execute(
            "UPDATE summaries SET suppressed_at = NULL WHERE summary_id = ?",
            ("summary_z",),
        )

        after = conn_with_vec0.execute(
            f"SELECT suppressed FROM {table} WHERE embedded_id = ?",
            ("summary_z",),
        ).fetchone()[0]
        assert after == 0

    def test_trigger_does_not_fire_for_unrelated_column_update(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        # The trigger is ``AFTER UPDATE OF suppressed_at`` — touching
        # other columns must not fire it.
        self._seed_summary_and_embedding(conn_with_vec0, "voyage-4-large", "summary_z")
        table = embeddings_table_name("voyage-4-large")
        # Touch a non-suppressed_at column.
        conn_with_vec0.execute(
            "UPDATE summaries SET content = 'changed' WHERE summary_id = ?",
            ("summary_z",),
        )
        suppressed = conn_with_vec0.execute(
            f"SELECT suppressed FROM {table} WHERE embedded_id = ?",
            ("summary_z",),
        ).fetchone()[0]
        assert suppressed == 0


@skip_if_no_vec0
class TestAfterDeleteTrigger:
    """AFTER DELETE ON summaries cascades to vec0 row removal."""

    def test_delete_summary_removes_vec0_row(self, conn_with_vec0: sqlite3.Connection) -> None:
        conn_with_vec0.execute(
            "INSERT OR IGNORE INTO conversations (conversation_id, session_id) "
            "VALUES (1, 'session-test')"
        )
        conn_with_vec0.execute(
            "INSERT INTO summaries "
            "  (summary_id, conversation_id, kind, depth, content, "
            "   token_count, created_at) "
            "VALUES ('zz_summary', 1, 'leaf', 0, 'content', 10, "
            "        datetime('now'))"
        )
        register_embedding_profile(conn_with_vec0, "voyage-4-large", 3)
        ensure_embeddings_table(conn_with_vec0, "voyage-4-large", 3)
        record_embedding(
            conn_with_vec0,
            model_name="voyage-4-large",
            embedded_id="zz_summary",
            embedded_kind="summary",
            vector=[0.1, 0.2, 0.3],
            source_token_count=1,
        )

        table = embeddings_table_name("voyage-4-large")
        before = conn_with_vec0.execute(
            f"SELECT COUNT(*) FROM {table} WHERE embedded_id = ?",
            ("zz_summary",),
        ).fetchone()[0]
        assert before == 1

        # DELETE the summary — the AFTER DELETE trigger should fire.
        conn_with_vec0.execute("DELETE FROM summaries WHERE summary_id = 'zz_summary'")

        after = conn_with_vec0.execute(
            f"SELECT COUNT(*) FROM {table} WHERE embedded_id = ?",
            ("zz_summary",),
        ).fetchone()[0]
        assert after == 0


# ===========================================================================
# try_load_sqlite_vec graceful path — uses connection.py helper
# ===========================================================================


class TestTryLoadSqliteVecGracefulPath:
    """``try_load_sqlite_vec`` — silent=True graceful degrade."""

    def test_silent_true_returns_bool_without_warning(self) -> None:
        # When sqlite-vec is loadable on this Python, ``try_load_sqlite_vec``
        # returns True. When it's not loadable (system Python edge case),
        # it returns False without raising. We test the API contract here;
        # the failure path is covered exhaustively by
        # tests/test_db_open_db.py.
        from lossless_hermes.db.connection import try_load_sqlite_vec

        conn = sqlite3.connect(":memory:")
        try:
            with closing(conn):
                result = try_load_sqlite_vec(conn, silent=True)
                # Result is a bool; either True (loadable) or False
                # (not loadable on this build).
                assert isinstance(result, bool)
                if VEC0_AVAILABLE:
                    assert result is True
        finally:
            # ``with closing(...)`` already handled close; no-op here.
            pass


# ---------------------------------------------------------------------------
# Suppress unused-import warning for typing aliases referenced via TYPE_CHECKING
# ---------------------------------------------------------------------------
_ = EmbeddedKind
