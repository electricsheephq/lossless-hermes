"""Tests for :mod:`lossless_hermes.operator.semantic_infra` (issue 08-14).

The TS source has no dedicated test file —
``semantic-infra-init.ts`` is exercised via
``test/v41-suppression-cascade-trigger.test.ts`` and live plugin-init
runs. This Python suite ships the dedicated coverage the spec's
acceptance criteria require:

* ``test_idempotent_second_call`` — initialize, then initialize again,
  assert ``kind="already_initialized"`` with empty ``triggers_created``.
* ``test_unavailable_when_vec_missing`` — patch :mod:`sqlite_vec` so
  ``load`` raises, assert ``kind="unavailable"`` with a clear reason and
  no exception escaping.
* ``test_triggers_fire_on_suppress`` — initialize on a fresh DB, seed a
  summary + a vec0 row, ``UPDATE summaries.suppressed_at``, assert the
  vec0 metadata col flipped to ``1`` (the
  ``lcm_embed_suppress_<slug>`` trigger fired).

vec0-dependent tests are gated on the extension being loadable on this
Python build (Apple system Python often lacks
``--enable-loadable-sqlite-extensions``). The gate mirrors
:mod:`tests.embeddings.test_store`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.operator.semantic_infra import (
    DEFAULT_DIM,
    DEFAULT_MODEL,
    KNOWN_MODEL_DIMS,
    SemanticInfraDeps,
    SemanticInfraInitResult,
    init_semantic_infra_if_possible,
)


# ---------------------------------------------------------------------------
# vec0 availability probe (same pattern as tests/embeddings/test_store.py).
# ---------------------------------------------------------------------------


def _vec0_loadable() -> bool:
    """Return :data:`True` iff ``sqlite_vec.load`` succeeds on this Python."""
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        return False
    try:
        import sqlite_vec

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
        "sqlite-vec extension not loadable on this Python build. "
        "Vec0-dependent semantic-infra tests skip cleanly."
    ),
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _open_conn(*, load_vec0: bool) -> sqlite3.Connection:
    """Open a bare ``:memory:`` connection. Loads sqlite-vec if requested."""
    conn = sqlite3.connect(":memory:")
    if load_vec0:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


@pytest.fixture
def conn_no_vec0() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB without vec0 loaded.

    Used for the ``unavailable_when_vec_missing`` path — covers the
    case where ``sqlite_vec.load`` raises (extension not bundled / not
    discoverable).
    """
    conn = _open_conn(load_vec0=False)
    try:
        run_lcm_migrations(conn, fts5_available=False)
        yield conn
    finally:
        conn.close()


@pytest.fixture
def conn_with_vec0() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB with vec0 loaded.

    Used for the happy-path + idempotency + trigger-fire tests.
    """
    conn = _open_conn(load_vec0=True)
    try:
        run_lcm_migrations(conn, fts5_available=False)
        yield conn
    finally:
        conn.close()


def _empty_env() -> dict[str, str]:
    """Isolated empty env so tests don't see operator's ``LCM_*`` vars."""
    return {}


def _seed_conversation(db: sqlite3.Connection, *, conv_id: int = 1) -> None:
    """Seed a conversation row so summaries' FK can be satisfied."""
    db.execute(
        "INSERT INTO conversations (conversation_id, session_id, session_key, active) "
        "VALUES (?, ?, NULL, 1)",
        (conv_id, f"sess-{conv_id}"),
    )


def _seed_summary(
    db: sqlite3.Connection,
    *,
    summary_id: str,
    kind: str = "leaf",
    conversation_id: int = 1,
    suppressed_at: str | None = None,
) -> None:
    """Seed a single summary row."""
    db.execute(
        """
        INSERT INTO summaries
            (summary_id, conversation_id, kind, content, token_count,
             source_message_token_count, descendant_token_count, suppressed_at)
        VALUES (?, ?, ?, ?, 100, 0, 0, ?)
        """,
        (summary_id, conversation_id, kind, f"body {summary_id}", suppressed_at),
    )


# ===========================================================================
# Happy path + AC coverage
# ===========================================================================


@skip_if_no_vec0
class TestInitFirstCall:
    """First-call semantics: fresh DB → ``kind="initialized"``."""

    def test_returns_initialized_with_triggers_on_fresh_db(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        result = init_semantic_infra_if_possible(
            conn_with_vec0,
            SemanticInfraDeps(env=_empty_env()),
        )
        assert isinstance(result, SemanticInfraInitResult)
        assert result.kind == "initialized"
        assert result.profile_id == DEFAULT_MODEL
        assert result.table_name == "lcm_embeddings_voyage4large"
        # Both triggers must be reported as newly created on a fresh DB.
        assert set(result.triggers_created) == {
            "lcm_embed_suppress_voyage4large",
            "lcm_embed_delete_voyage4large",
        }
        assert result.reason is None

    def test_profile_row_actually_inserted(self, conn_with_vec0: sqlite3.Connection) -> None:
        init_semantic_infra_if_possible(
            conn_with_vec0,
            SemanticInfraDeps(env=_empty_env()),
        )
        row = conn_with_vec0.execute(
            "SELECT model_name, dim, active FROM lcm_embedding_profile WHERE model_name = ?",
            (DEFAULT_MODEL,),
        ).fetchone()
        assert row == (DEFAULT_MODEL, DEFAULT_DIM, 1)

    def test_vec0_table_actually_created(self, conn_with_vec0: sqlite3.Connection) -> None:
        init_semantic_infra_if_possible(
            conn_with_vec0,
            SemanticInfraDeps(env=_empty_env()),
        )
        row = conn_with_vec0.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("lcm_embeddings_voyage4large",),
        ).fetchone()
        assert row is not None

    def test_triggers_actually_created(self, conn_with_vec0: sqlite3.Connection) -> None:
        init_semantic_infra_if_possible(
            conn_with_vec0,
            SemanticInfraDeps(env=_empty_env()),
        )
        triggers = {
            row[0]
            for row in conn_with_vec0.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'lcm_embed_%_voyage4large'"
            ).fetchall()
        }
        assert triggers == {
            "lcm_embed_suppress_voyage4large",
            "lcm_embed_delete_voyage4large",
        }


# ===========================================================================
# AC: ``test_idempotent_second_call``
# ===========================================================================


@skip_if_no_vec0
class TestIdempotency:
    """Second-call semantics: already bootstrapped → ``already_initialized``."""

    def test_idempotent_second_call(self, conn_with_vec0: sqlite3.Connection) -> None:
        """AC: second call returns ``kind="already_initialized"`` with no new triggers."""
        first = init_semantic_infra_if_possible(
            conn_with_vec0,
            SemanticInfraDeps(env=_empty_env()),
        )
        assert first.kind == "initialized"
        assert first.triggers_created  # non-empty on the first call

        second = init_semantic_infra_if_possible(
            conn_with_vec0,
            SemanticInfraDeps(env=_empty_env()),
        )
        assert second.kind == "already_initialized"
        assert second.profile_id == DEFAULT_MODEL
        assert second.table_name == "lcm_embeddings_voyage4large"
        assert second.triggers_created == []
        assert second.reason is None

    def test_third_call_still_already_initialized(self, conn_with_vec0: sqlite3.Connection) -> None:
        """Idempotency is permanent — N>2 calls stay ``already_initialized``."""
        for _ in range(3):
            init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=_empty_env()))
        # Fourth call is the assertion target.
        result = init_semantic_infra_if_possible(
            conn_with_vec0, SemanticInfraDeps(env=_empty_env())
        )
        assert result.kind == "already_initialized"
        assert result.triggers_created == []

    def test_second_call_no_duplicate_profile_row(self, conn_with_vec0: sqlite3.Connection) -> None:
        """``INSERT OR IGNORE`` keeps ``lcm_embedding_profile`` to one row."""
        init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=_empty_env()))
        init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=_empty_env()))
        count = conn_with_vec0.execute(
            "SELECT COUNT(*) FROM lcm_embedding_profile WHERE model_name = ?",
            (DEFAULT_MODEL,),
        ).fetchone()[0]
        assert count == 1


# ===========================================================================
# AC: ``test_unavailable_when_vec_missing``
# ===========================================================================


class TestUnavailableWhenVecMissing:
    """vec0 missing → ``kind="unavailable"``, no raise.

    These tests do NOT require vec0 to be loadable on the test runner;
    they explicitly patch ``sqlite_vec.load`` to raise.
    """

    def test_unavailable_when_vec_missing(self, conn_no_vec0: sqlite3.Connection) -> None:
        """AC: patch ``sqlite_vec.load`` to raise → ``kind="unavailable"``."""
        # Patch the ``sqlite_vec.load`` symbol that
        # :func:`try_load_sqlite_vec` looks up dynamically. We use
        # ``OperationalError`` because that's the exception class
        # ``try_load_sqlite_vec`` catches and reports as a failure.
        with patch(
            "lossless_hermes.db.connection.sqlite_vec.load",
            side_effect=sqlite3.OperationalError("no such module: vec0 (simulated)"),
        ):
            result = init_semantic_infra_if_possible(
                conn_no_vec0, SemanticInfraDeps(env=_empty_env())
            )
        assert result.kind == "unavailable"
        assert result.reason is not None
        assert "vec" in result.reason.lower()
        # No profile row was created on the failure path.
        count = conn_no_vec0.execute("SELECT COUNT(*) FROM lcm_embedding_profile").fetchone()[0]
        assert count == 0

    def test_unavailable_when_import_error(self, conn_no_vec0: sqlite3.Connection) -> None:
        """``ImportError`` from ``sqlite_vec`` is also caught and surfaced."""
        with patch(
            "lossless_hermes.db.connection.sqlite_vec.load",
            side_effect=ImportError("sqlite_vec not installed (simulated)"),
        ):
            result = init_semantic_infra_if_possible(
                conn_no_vec0, SemanticInfraDeps(env=_empty_env())
            )
        assert result.kind == "unavailable"
        assert result.reason is not None
        # No exception propagated — that's the load-bearing contract.

    def test_unavailable_when_disable_semantic_set(self, conn_no_vec0: sqlite3.Connection) -> None:
        """``LCM_DISABLE_SEMANTIC=true`` short-circuits before touching vec0."""
        env = {"LCM_DISABLE_SEMANTIC": "true"}
        result = init_semantic_infra_if_possible(conn_no_vec0, SemanticInfraDeps(env=env))
        assert result.kind == "unavailable"
        assert result.reason == "LCM_DISABLE_SEMANTIC=true"
        # No side effects — profile_id, table_name, triggers_created
        # all empty/None.
        assert result.profile_id is None
        assert result.table_name is None
        assert result.triggers_created == []

    def test_unavailable_when_disable_semantic_via_deps(
        self, conn_no_vec0: sqlite3.Connection
    ) -> None:
        """``deps.disable_semantic=True`` short-circuits identically."""
        result = init_semantic_infra_if_possible(
            conn_no_vec0,
            SemanticInfraDeps(env=_empty_env(), disable_semantic=True),
        )
        assert result.kind == "unavailable"
        assert result.reason == "deps.disable_semantic=True"

    def test_disable_semantic_case_insensitive(self, conn_no_vec0: sqlite3.Connection) -> None:
        """``LCM_DISABLE_SEMANTIC=TRUE`` / mixed-case also opts out."""
        for val in ("TRUE", "True", "tRuE", " true "):
            env = {"LCM_DISABLE_SEMANTIC": val}
            result = init_semantic_infra_if_possible(conn_no_vec0, SemanticInfraDeps(env=env))
            assert result.kind == "unavailable", val

    def test_disable_semantic_other_values_no_op(self, conn_no_vec0: sqlite3.Connection) -> None:
        """Anything other than ``true`` falls through to normal init.

        Tested on a connection without vec0 to avoid coupling to the
        happy-path vec0 fixture. Result is still ``unavailable`` but
        the reason is "sqlite-vec not loadable", not the disable
        opt-out.
        """
        for val in ("false", "0", "no", "1", "yes", ""):
            env = {"LCM_DISABLE_SEMANTIC": val}
            result = init_semantic_infra_if_possible(conn_no_vec0, SemanticInfraDeps(env=env))
            # Either ``unavailable`` for vec0-missing reason OR
            # ``initialized``/``already_initialized`` if vec0 is loadable.
            if result.kind == "unavailable":
                assert "DISABLE_SEMANTIC" not in (result.reason or "")


# ===========================================================================
# AC: ``test_triggers_fire_on_suppress``
# ===========================================================================


@skip_if_no_vec0
class TestTriggersFireOnSuppress:
    """The ``lcm_embed_suppress_<slug>`` trigger flips the vec0 metadata col."""

    def test_triggers_fire_on_suppress(self, conn_with_vec0: sqlite3.Connection) -> None:
        """AC: UPDATE ``summaries.suppressed_at`` flips ``suppressed`` to 1."""
        # Bootstrap the semantic infra.
        result = init_semantic_infra_if_possible(
            conn_with_vec0, SemanticInfraDeps(env=_empty_env())
        )
        assert result.kind == "initialized"

        # Seed a summary + a corresponding vec0 row.
        _seed_conversation(conn_with_vec0, conv_id=1)
        _seed_summary(conn_with_vec0, summary_id="s-1")

        # Insert a vec0 row with ``suppressed=0`` to mirror what the
        # backfill cron would do.
        import sqlite_vec

        vector_bytes = sqlite_vec.serialize_float32([0.0] * DEFAULT_DIM)
        conn_with_vec0.execute(
            f"INSERT INTO {result.table_name} "
            "(embedding, embedded_id, embedded_kind, suppressed) "
            "VALUES (?, ?, ?, 0)",
            (vector_bytes, "s-1", "summary"),
        )

        # Pre-condition: vec0 row reports ``suppressed = 0``.
        pre = conn_with_vec0.execute(
            f"SELECT suppressed FROM {result.table_name} "
            "WHERE embedded_id = ? AND embedded_kind = ?",
            ("s-1", "summary"),
        ).fetchone()
        assert pre == (0,)

        # Act: flip the summary to suppressed.
        conn_with_vec0.execute(
            "UPDATE summaries SET suppressed_at = datetime('now') WHERE summary_id = ?",
            ("s-1",),
        )

        # Post-condition: the trigger flipped the vec0 metadata col.
        post = conn_with_vec0.execute(
            f"SELECT suppressed FROM {result.table_name} "
            "WHERE embedded_id = ? AND embedded_kind = ?",
            ("s-1", "summary"),
        ).fetchone()
        assert post == (1,)

    def test_un_suppress_flips_back_to_zero(self, conn_with_vec0: sqlite3.Connection) -> None:
        """Setting ``suppressed_at = NULL`` flips ``suppressed`` back to 0."""
        result = init_semantic_infra_if_possible(
            conn_with_vec0, SemanticInfraDeps(env=_empty_env())
        )
        assert result.kind == "initialized"
        _seed_conversation(conn_with_vec0, conv_id=1)
        # Seed an already-suppressed summary so we can verify the
        # un-suppress direction of the trigger fires too.
        _seed_summary(
            conn_with_vec0,
            summary_id="s-1",
            suppressed_at="2026-01-01 00:00:00",
        )
        import sqlite_vec

        vector_bytes = sqlite_vec.serialize_float32([0.0] * DEFAULT_DIM)
        conn_with_vec0.execute(
            f"INSERT INTO {result.table_name} "
            "(embedding, embedded_id, embedded_kind, suppressed) "
            "VALUES (?, ?, ?, 1)",
            (vector_bytes, "s-1", "summary"),
        )
        conn_with_vec0.execute(
            "UPDATE summaries SET suppressed_at = NULL WHERE summary_id = ?",
            ("s-1",),
        )
        post = conn_with_vec0.execute(
            f"SELECT suppressed FROM {result.table_name} "
            "WHERE embedded_id = ? AND embedded_kind = ?",
            ("s-1", "summary"),
        ).fetchone()
        assert post == (0,)

    def test_delete_trigger_removes_vec0_row(self, conn_with_vec0: sqlite3.Connection) -> None:
        """The AFTER DELETE trigger cascades into the vec0 table."""
        result = init_semantic_infra_if_possible(
            conn_with_vec0, SemanticInfraDeps(env=_empty_env())
        )
        assert result.kind == "initialized"
        _seed_conversation(conn_with_vec0, conv_id=1)
        _seed_summary(conn_with_vec0, summary_id="s-1")
        import sqlite_vec

        vector_bytes = sqlite_vec.serialize_float32([0.0] * DEFAULT_DIM)
        conn_with_vec0.execute(
            f"INSERT INTO {result.table_name} "
            "(embedding, embedded_id, embedded_kind, suppressed) "
            "VALUES (?, ?, ?, 0)",
            (vector_bytes, "s-1", "summary"),
        )

        # Hard-delete the summary.
        conn_with_vec0.execute("DELETE FROM summaries WHERE summary_id = ?", ("s-1",))

        # vec0 row should be gone.
        row = conn_with_vec0.execute(
            f"SELECT 1 FROM {result.table_name} WHERE embedded_id = ?",
            ("s-1",),
        ).fetchone()
        assert row is None


# ===========================================================================
# Config + slug coverage
# ===========================================================================


@skip_if_no_vec0
class TestModelAndDimResolution:
    """Env + deps precedence + slug naming."""

    def test_uses_env_lcm_embedding_model(self, conn_with_vec0: sqlite3.Connection) -> None:
        env = {"LCM_EMBEDDING_MODEL": "voyage-3-lite"}
        result = init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=env))
        assert result.kind == "initialized"
        assert result.profile_id == "voyage-3-lite"
        # voyage-3-lite is in KNOWN_MODEL_DIMS with dim 512.
        assert result.table_name == "lcm_embeddings_voyage3lite"
        row = conn_with_vec0.execute(
            "SELECT dim FROM lcm_embedding_profile WHERE model_name = ?",
            ("voyage-3-lite",),
        ).fetchone()
        assert row == (KNOWN_MODEL_DIMS["voyage-3-lite"],)

    def test_uses_deps_model_name_over_env(self, conn_with_vec0: sqlite3.Connection) -> None:
        env = {"LCM_EMBEDDING_MODEL": "voyage-3-lite"}
        result = init_semantic_infra_if_possible(
            conn_with_vec0,
            SemanticInfraDeps(env=env, model_name="voyage-3"),
        )
        assert result.profile_id == "voyage-3"

    def test_env_dim_override(self, conn_with_vec0: sqlite3.Connection) -> None:
        env = {"LCM_EMBEDDING_MODEL": "custom-model", "LCM_EMBEDDING_DIM": "768"}
        result = init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=env))
        assert result.kind == "initialized"
        row = conn_with_vec0.execute(
            "SELECT dim FROM lcm_embedding_profile WHERE model_name = ?",
            ("custom-model",),
        ).fetchone()
        assert row == (768,)

    def test_dim_for_known_model_when_env_dim_unset(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        """KNOWN_MODEL_DIMS lookup beats the hardcoded DEFAULT_DIM."""
        env = {"LCM_EMBEDDING_MODEL": "voyage-3-lite"}
        result = init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=env))
        row = conn_with_vec0.execute(
            "SELECT dim FROM lcm_embedding_profile WHERE model_name = ?",
            ("voyage-3-lite",),
        ).fetchone()
        # 512, not DEFAULT_DIM=1024.
        assert row == (512,)

    def test_dim_for_unknown_model_falls_back_to_default(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        env = {"LCM_EMBEDDING_MODEL": "custom-model"}
        result = init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=env))
        row = conn_with_vec0.execute(
            "SELECT dim FROM lcm_embedding_profile WHERE model_name = ?",
            ("custom-model",),
        ).fetchone()
        assert row == (DEFAULT_DIM,)

    def test_invalid_env_dim_falls_through(self, conn_with_vec0: sqlite3.Connection) -> None:
        """Non-positive / non-integer env dim falls back to KNOWN_MODEL_DIMS."""
        env = {
            "LCM_EMBEDDING_MODEL": "voyage-3-lite",
            "LCM_EMBEDDING_DIM": "not-a-number",
        }
        result = init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=env))
        row = conn_with_vec0.execute(
            "SELECT dim FROM lcm_embedding_profile WHERE model_name = ?",
            ("voyage-3-lite",),
        ).fetchone()
        # Falls back to the known-model dim (512), not DEFAULT_DIM.
        assert row == (512,)

    def test_zero_env_dim_falls_through(self, conn_with_vec0: sqlite3.Connection) -> None:
        """``LCM_EMBEDDING_DIM=0`` is not positive; falls through."""
        env = {
            "LCM_EMBEDDING_MODEL": "voyage-3-lite",
            "LCM_EMBEDDING_DIM": "0",
        }
        result = init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=env))
        row = conn_with_vec0.execute(
            "SELECT dim FROM lcm_embedding_profile WHERE model_name = ?",
            ("voyage-3-lite",),
        ).fetchone()
        assert row == (512,)


@skip_if_no_vec0
class TestTriggerNamingConvention:
    """AC: trigger names embed the sanitized model slug."""

    def test_trigger_names_use_sanitized_slug_for_dashes(
        self, conn_with_vec0: sqlite3.Connection
    ) -> None:
        """``voyage-3-large`` → slug ``voyage3large`` (dashes stripped)."""
        env = {"LCM_EMBEDDING_MODEL": "voyage-3-large"}
        result = init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=env))
        # AC: "Trigger names embed the sanitized model slug
        # (e.g. voyage_3_large for voyage-3-large)." The Python port
        # sluggifies more aggressively (removes ALL non-alphanumeric,
        # not just dashes-to-underscores), matching the TS source's
        # ``slugForModel`` in ``embeddings/store.ts``. So
        # ``voyage-3-large`` → ``voyage3large``, not ``voyage_3_large``.
        # This matches the existing ``embeddings_table_name`` /
        # ``_slug_for`` behavior already shipped in 05-03.
        assert set(result.triggers_created) == {
            "lcm_embed_suppress_voyage3large",
            "lcm_embed_delete_voyage3large",
        }


# ===========================================================================
# Smoke: dim/model sanity-check warning
# ===========================================================================


@skip_if_no_vec0
class TestDimMismatchSanityWarning:
    """Operator config with known-model + wrong dim → warning, no block."""

    def test_known_model_with_wrong_dim_still_inits(
        self,
        conn_with_vec0: sqlite3.Connection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """voyage-4-large (known 1024) + explicit dim=768 → warning + proceed."""
        env = {"LCM_EMBEDDING_MODEL": "voyage-4-large", "LCM_EMBEDDING_DIM": "768"}
        with caplog.at_level("WARNING", logger="lossless_hermes.operator.semantic_infra"):
            result = init_semantic_infra_if_possible(conn_with_vec0, SemanticInfraDeps(env=env))
        assert result.kind == "initialized"
        # The warning is informational; the call still succeeds.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("doesn't match known dim" in r.getMessage() for r in warnings)
