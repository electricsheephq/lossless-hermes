"""Tests for ``_LifecycleMixin`` bodies (issue 02-03).

Covers the heavy-init / tear-down / reset cluster filled in by
issue 02-03 on top of the 02-01 mixin skeleton:

* :meth:`_LifecycleMixin.on_session_start` opens the DB at the
  ADR-002 canonical path (``$HERMES_HOME/lossless-hermes/lcm.db``),
  runs migrations, instantiates the four Epic-01 stores.
* The Apple-system-Python sqlite-extension guard fires BEFORE the
  first DB open attempt — regression guard against re-introducing
  the guard call to :meth:`LCMEngine.__init__` (which would block
  perfectly-working Python installations).
* :meth:`_LifecycleMixin.on_session_end` closes the DB and clears
  every store reference. Idempotent (safe to call twice).
* :meth:`_LifecycleMixin.on_session_reset` zeroes the four ABC
  token-state fields AND clears the diff-ingest cursor
  (``_last_seen_message_idx``); does NOT close the DB.
* Multiple ``on_session_start`` calls on the same engine instance
  for different session_ids share one DB connection (per-process
  idempotence — re-opening would churn the WAL and wastefully
  re-run the migration ladder).

The 00-06 and 02-01 regression suites still apply — those tests
exercise the construction-time invariants and the no-op-mixin
passthroughs preserved through 02-03.

See:

* ``docs/adr/001-plugin-distribution-model.md`` §Consequences —
  "heavy init in ``on_session_start``".
* ``docs/adr/002-plugin-data-directory.md`` §"Option A" —
  ``$HERMES_HOME/lossless-hermes/lcm.db`` path.
* ``docs/adr/004-sqlite3-backend.md`` §Consequences — Apple
  system Python guard policy.
* ``epics/02-engine-skeleton/02-03-on-session-lifecycle.md`` —
  this issue's AC.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.store.compaction_maintenance import CompactionMaintenanceStore
from lossless_hermes.store.compaction_telemetry import CompactionTelemetryStore
from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.store.summary import SummaryStore

# ---------------------------------------------------------------------------
# Skip marker: actions/setup-python macOS builds lack enable_load_extension
# ---------------------------------------------------------------------------
#
# Per ADR-004 §Open questions item 1 and ADR-028 §Decision point 8, the
# actions/setup-python macOS pre-built CPython ships without
# ``--enable-loadable-sqlite-extensions``. ``on_session_start`` opens an
# ``open_lcm_db()`` connection that loads sqlite-vec, so the Apple-Python
# guard fires and raises before any test assertions can run on those
# cells. The guard-introspection tests below still run — they monkey-
# patch ``_has_sqlite_extension_loading`` rather than depending on the
# OS-level capability — so this skip targets only the DB-opening tests.
#
# Ubuntu cells + Homebrew/pyenv/uv-managed Python all have extension
# loading enabled, so the skip only fires on the macOS GH-Actions runners.
# Mirrors ``_skip_no_extension_loading`` in ``tests/test_db_connection.py``.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. "
        "Apple-Python-guard tests still run (they monkey-patch the "
        "introspection hook). See ADR-004 §Open questions item 1 + "
        "ADR-028 §Decision point 8."
    ),
)


# ---------------------------------------------------------------------------
# on_session_start — opens DB, runs migrations, instantiates stores
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_on_session_start_opens_db_at_canonical_path(tmp_home: Path) -> None:
    """ADR-002 §Option A: DB lives at ``$HERMES_HOME/lossless-hermes/lcm.db``.

    With a bare :class:`LcmConfig` (``database_path=""``), the engine
    falls back to that canonical location, creating intermediate
    directories on the way (``open_lcm_db``'s ``mkdir -p`` semantics).
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    try:
        engine.on_session_start("sess-1")

        expected = tmp_home / ".hermes" / "lossless-hermes" / "lcm.db"
        assert expected.exists(), f"DB not created at {expected}"
        assert engine._db is not None
    finally:
        engine.on_session_end("sess-1", [])


@_skip_no_extension_loading
def test_on_session_start_honors_explicit_database_path(tmp_home: Path) -> None:
    """``config.database_path`` overrides the canonical fallback.

    Production callers (the config resolver in :mod:`db.config`) fill
    in ``database_path`` from env + ``config.yaml``; the engine just
    uses what it's given.
    """
    custom = tmp_home / "custom-location" / "my.db"
    cfg = LcmConfig(database_path=str(custom))
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=cfg)
    try:
        engine.on_session_start("sess-1")
        assert custom.exists(), f"DB not created at custom path {custom}"
        # And the canonical fallback path was NOT created.
        canonical = tmp_home / ".hermes" / "lossless-hermes" / "lcm.db"
        assert not canonical.exists(), (
            f"Canonical path created when database_path was overridden: {canonical}"
        )
    finally:
        engine.on_session_end("sess-1", [])


@_skip_no_extension_loading
def test_on_session_start_runs_migrations(tmp_home: Path) -> None:
    """The migration ladder runs once on first ``on_session_start``.

    Verified by checking that the ``conversations`` table exists on
    the opened DB — that table is one of the 12 core tables created
    by ``_ensure_core_tables`` (the first ladder step).
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    try:
        engine.on_session_start("sess-1")

        row = engine._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations'"
        ).fetchone()
        assert row is not None, "Migrations did not create conversations table"
    finally:
        engine.on_session_end("sess-1", [])


@_skip_no_extension_loading
def test_on_session_start_migrations_are_idempotent(tmp_home: Path) -> None:
    """A second ``on_session_start`` on the same engine is a no-op DB-open.

    ``self._db`` already points at the open connection; we must not
    re-open it (would churn the WAL, lose test-fixture state, and
    re-walk the migration ladder unnecessarily).
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    try:
        engine.on_session_start("sess-1")
        db_before = engine._db
        store_before = engine._conversation_store

        engine.on_session_start("sess-2")  # different session_id, same engine
        # The DB and stores are the same instances — no re-open.
        assert engine._db is db_before
        assert engine._conversation_store is store_before
    finally:
        engine.on_session_end("sess-1", [])


@_skip_no_extension_loading
def test_on_session_start_instantiates_all_four_stores(tmp_home: Path) -> None:
    """ADR-027 §Consequences: state lives on shell, mixins consume it.

    After ``on_session_start`` the four store attributes are wired so
    downstream Epic 02-04 mixin bodies can do
    ``self._conversation_store.<method>(...)``.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    try:
        engine.on_session_start("sess-1")

        assert isinstance(engine._conversation_store, ConversationStore)
        assert isinstance(engine._summary_store, SummaryStore)
        assert isinstance(engine._telemetry_store, CompactionTelemetryStore)
        assert isinstance(engine._maintenance_store, CompactionMaintenanceStore)
    finally:
        engine.on_session_end("sess-1", [])


@_skip_no_extension_loading
def test_on_session_start_creates_parent_directory(tmp_home: Path) -> None:
    """The ``$HERMES_HOME/lossless-hermes/`` dir is created by ``open_lcm_db``."""
    # Pre-condition: only ``.hermes`` exists (created by tmp_home fixture).
    lh_dir = tmp_home / ".hermes" / "lossless-hermes"
    assert not lh_dir.exists()

    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    try:
        engine.on_session_start("sess-1")
        assert lh_dir.is_dir(), "lossless-hermes/ subdir was not created"
    finally:
        engine.on_session_end("sess-1", [])


# ---------------------------------------------------------------------------
# Apple-system-Python guard — fires BEFORE DB open
# ---------------------------------------------------------------------------


def test_apple_python_guard_fires_before_db_open(
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-004 §Consequences: guard fires before the first DB open.

    Monkey-patch :func:`_has_sqlite_extension_loading` (the
    introspection hook the guard consults — ``sqlite3.Connection`` is
    C-immutable so we can't ``delattr`` it directly) and call
    ``on_session_start``. The guard must fire, raising
    :class:`RuntimeError`. The DB file must NOT have been created
    (the guard fires earlier than ``open_lcm_db``'s ``mkdir -p``).
    """
    import lossless_hermes.engine as engine_mod

    monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)

    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    with pytest.raises(RuntimeError, match=r"sqlite3.Connection.enable_load_extension"):
        engine.on_session_start("sess-1")

    # DB was NOT created — guard short-circuited before mkdir.
    canonical = tmp_home / ".hermes" / "lossless-hermes" / "lcm.db"
    assert not canonical.exists(), f"DB created despite Apple-Python guard firing: {canonical}"

    # And the engine's DB attribute is still None — guarded against
    # half-state.
    assert engine._db is None
    assert engine._conversation_store is None


def test_apple_python_guard_does_not_fire_at_construction(
    tmp_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """02-01 invariant preserved at 02-03: ``__init__`` does not call the guard.

    The guard MUST defer to ``on_session_start`` per ADR-001
    §Consequences (heavy init belongs in ``on_session_start``). If a
    refactor re-adds ``_check_sqlite_extension_loading()`` to
    ``__init__``, this test would catch it.
    """
    import lossless_hermes.engine as engine_mod

    monkeypatch.setattr(engine_mod, "_has_sqlite_extension_loading", lambda: False)

    # No raise on construction.
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    assert engine.name == "lcm"


# ---------------------------------------------------------------------------
# on_session_end — closes DB, clears store refs, idempotent
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_on_session_end_closes_db(tmp_home: Path) -> None:
    """``on_session_end`` closes the connection via the sanctioned factory."""
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine.on_session_start("sess-1")
    assert engine._db is not None

    engine.on_session_end("sess-1", [])
    assert engine._db is None


@_skip_no_extension_loading
def test_on_session_end_clears_all_store_references(tmp_home: Path) -> None:
    """After teardown every store reference is ``None`` again."""
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine.on_session_start("sess-1")
    assert engine._conversation_store is not None

    engine.on_session_end("sess-1", [])
    assert engine._conversation_store is None
    assert engine._summary_store is None
    assert engine._telemetry_store is None
    assert engine._maintenance_store is None


@_skip_no_extension_loading
def test_on_session_end_is_idempotent_after_close(tmp_home: Path) -> None:
    """Calling ``on_session_end`` twice is safe (no crash)."""
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine.on_session_start("sess-1")
    engine.on_session_end("sess-1", [])
    # Second call is a no-op.
    engine.on_session_end("sess-1", [])
    assert engine._db is None


def test_on_session_end_idempotent_without_start(tmp_home: Path) -> None:
    """Calling ``on_session_end`` without a prior ``on_session_start`` is safe.

    The construction-time invariant is ``self._db is None``, so the
    method's early-return branch handles this cleanly. Per the
    ContextEngine ABC docstring ("Use this to flush state, close DB
    connections, etc."), the contract is symmetric — a Hermes that
    fires ``on_session_end`` for an engine that never saw
    ``on_session_start`` (e.g. immediate ``Ctrl-C`` after plugin
    register) must not crash.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    # No on_session_start.
    engine.on_session_end("sess-1", [{"role": "user", "content": "hi"}])
    # Still None — we never opened, so nothing to close.
    assert engine._db is None


# ---------------------------------------------------------------------------
# on_session_reset — zeroes tokens + clears cursors, keeps DB open
# ---------------------------------------------------------------------------


def test_on_session_reset_zeroes_token_state(tmp_home: Path) -> None:
    """The ABC contract: ``on_session_reset`` zeroes the four token fields."""
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine.last_prompt_tokens = 100
    engine.last_completion_tokens = 50
    engine.last_total_tokens = 150
    engine.compression_count = 3

    engine.on_session_reset()

    assert engine.last_prompt_tokens == 0
    assert engine.last_completion_tokens == 0
    assert engine.last_total_tokens == 0
    assert engine.compression_count == 0


def test_on_session_reset_clears_last_seen_message_idx(tmp_home: Path) -> None:
    """LCM-specific: the diff-ingest cursor is cleared too."""
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine._last_seen_message_idx["sess-1"] = 42
    engine._last_seen_message_idx["sess-2"] = 17

    engine.on_session_reset()

    assert engine._last_seen_message_idx == {}


@_skip_no_extension_loading
def test_on_session_reset_does_not_close_db(tmp_home: Path) -> None:
    """``/reset`` is within-process — DB connection stays open."""
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    try:
        engine.on_session_start("sess-1")
        db_before = engine._db
        assert db_before is not None

        engine.on_session_reset()

        # Same connection — not closed.
        assert engine._db is db_before
        assert engine._conversation_store is not None
    finally:
        engine.on_session_end("sess-1", [])


def test_on_session_reset_without_start(tmp_home: Path) -> None:
    """Reset before any session_start is a safe no-op on state.

    The ABC defaults already start at 0, so the reset is observable
    only through ``_last_seen_message_idx`` clearing (which is
    already empty on a fresh engine).
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine.on_session_reset()
    assert engine.last_prompt_tokens == 0
    assert engine._last_seen_message_idx == {}


# ---------------------------------------------------------------------------
# Full lifecycle round-trip
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_full_lifecycle_round_trip(tmp_home: Path) -> None:
    """A full session: start → use → end → start-again works correctly.

    Confirms the engine can serve a new session after a complete
    tear-down (the second ``on_session_start`` re-opens the DB).
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())

    # First session.
    engine.on_session_start("sess-1")
    assert engine._db is not None
    engine.on_session_end("sess-1", [])
    assert engine._db is None

    # Second session — re-uses the same DB file but a fresh connection.
    engine.on_session_start("sess-2")
    assert engine._db is not None
    assert isinstance(engine._conversation_store, ConversationStore)
    engine.on_session_end("sess-2", [])
    assert engine._db is None


@_skip_no_extension_loading
def test_db_path_relative_to_hermes_home_param(tmp_home: Path) -> None:
    """``hermes_home`` from the constructor is what gets used.

    Regression guard against a refactor that uses
    ``Path.home() / ".hermes"`` instead of the constructor arg
    (which would defeat ``tmp_home``-isolated tests on developer
    machines with a populated ``~/.hermes/``).
    """
    custom_home = tmp_home / "custom-hermes-home"
    engine = LCMEngine(hermes_home=custom_home, config=LcmConfig())
    try:
        engine.on_session_start("sess-1")
        expected = custom_home / "lossless-hermes" / "lcm.db"
        assert expected.exists()
    finally:
        engine.on_session_end("sess-1", [])
