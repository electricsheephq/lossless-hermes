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
# current_session_id tracking (issue 08-02 dependency)
# ---------------------------------------------------------------------------


def test_current_session_id_starts_as_none(tmp_home: Path) -> None:
    """Fresh engine — ``current_session_id is None`` before any session_start.

    Epic 08-02 ``/lcm status`` reads this attribute to decide whether
    to render the per-conversation block. The default ``None`` is the
    "no active conversation" marker.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    assert engine.current_session_id is None


@_skip_no_extension_loading
def test_on_session_start_sets_current_session_id(tmp_home: Path) -> None:
    """``on_session_start`` sets ``current_session_id`` for Epic 08 consumers.

    Per ``docs/porting-guides/plugin-glue.md`` §"Per-subcommand
    translation table" line 650, this field replaces the TS
    ``ctx.sessionId`` for ``/lcm`` handlers.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    try:
        engine.on_session_start("sess-abc-123")
        assert engine.current_session_id == "sess-abc-123"
    finally:
        engine.on_session_end("sess-abc-123", [])


@_skip_no_extension_loading
def test_on_session_start_updates_current_session_id_on_reentrant_call(
    tmp_home: Path,
) -> None:
    """Subsequent ``on_session_start`` for a new session updates the field.

    The DB stays open (idempotence preserves the connection), but
    Epic 08's ``/lcm status`` must reflect the most recent session
    — otherwise a CLI restart on the same process would show stats
    for the old session.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    try:
        engine.on_session_start("sess-1")
        assert engine.current_session_id == "sess-1"

        # Re-entrant call — DB stays open, but session id flips.
        engine.on_session_start("sess-2")
        assert engine.current_session_id == "sess-2"
    finally:
        engine.on_session_end("sess-2", [])


@_skip_no_extension_loading
def test_on_session_end_clears_current_session_id(tmp_home: Path) -> None:
    """``on_session_end`` clears the tracked session id.

    Symmetric with the DB-close path: after tear-down, status should
    report "no active conversation" rather than the stale prior
    session_id.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine.on_session_start("sess-1")
    assert engine.current_session_id == "sess-1"

    engine.on_session_end("sess-1", [])
    assert engine.current_session_id is None


def test_on_session_start_empty_session_id_yields_none(tmp_home: Path) -> None:
    """``on_session_start("")`` → ``current_session_id`` stays ``None``.

    Defensive: a Hermes mis-fire passing an empty string must not
    leave the engine in a "I have an active session of empty string"
    state. Empty/whitespace → ``None``.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    # Don't actually open the DB — the Apple-Python guard would fire
    # on macOS GHA runners and the test focus is purely the field
    # assignment. We use a subclass-style monkey-patch to skip the
    # DB-open path.
    import lossless_hermes.engine as engine_mod

    # We can't easily skip the DB open while preserving the assignment;
    # instead, use the @_skip_no_extension_loading marker so this only
    # runs where the DB opens. The empty-string assignment path runs
    # AFTER the DB check but BEFORE the open, so we still see the
    # assignment even if open fails — but to keep the test deterministic
    # we just check the assignment was applied before the open succeeded.
    if not hasattr(engine_mod.sqlite3.Connection, "enable_load_extension"):
        pytest.skip("Test requires DB open to verify assignment timing")

    try:
        engine.on_session_start("")
        # Empty string normalizes to None.
        assert engine.current_session_id is None
    finally:
        engine.on_session_end("", [])


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


# ---------------------------------------------------------------------------
# update_model — mid-session model switch (v0.1.1 P0 fix)
# ---------------------------------------------------------------------------
#
# Hermes's run_agent.py calls ``context_compressor.update_model(...)`` at
# seven sites. Two of them — the LM-Studio context preload
# (run_agent.py:2587) and the in-place model switch (run_agent.py:2728) —
# pass an extra ``api_mode=`` keyword that the ContextEngine ABC's default
# ``update_model`` (agent/context_engine.py:191) does NOT declare. Before
# the v0.1.1 fix, ``LCMEngine`` inherited that 5-param default and a
# ``/model`` switch raised ``TypeError: update_model() got an unexpected
# keyword argument 'api_mode'``. The override added in
# ``engine/lifecycle.py`` absorbs ``api_mode`` (+ ``**kwargs``) and
# delegates the budget recalculation to the ABC default.
#
# These tests construct a bare engine (no DB open) — ``update_model``
# touches only the in-memory ``context_length`` / ``threshold_tokens``
# fields, so no ``@_skip_no_extension_loading`` marker is needed.


def test_update_model_accepts_host_call_shape_with_api_mode(tmp_home: Path) -> None:
    """The exact run_agent.py:2728 call shape must not raise.

    This is the v0.1.1 P0 regression: ``run_agent.py``'s ``switch_model``
    path calls ``update_model`` with EVERY argument passed by keyword,
    including ``api_mode=self.api_mode``. Reproduce that call shape
    byte-for-byte and assert no exception — pre-fix this raised
    ``TypeError: ... unexpected keyword argument 'api_mode'``.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())

    # Byte-for-byte the run_agent.py:2728 keyword-call shape.
    engine.update_model(
        model="claude-opus-4",
        context_length=200_000,
        base_url="https://api.anthropic.com",
        api_key="sk-test",
        provider="anthropic",
        api_mode="responses",
    )

    # The ABC default still ran: context_length is updated and
    # threshold_tokens is re-derived from threshold_percent (0.75).
    assert engine.context_length == 200_000
    assert engine.threshold_tokens == int(200_000 * engine.threshold_percent)


def test_update_model_lmstudio_preload_call_shape(tmp_home: Path) -> None:
    """The run_agent.py:2587 LM-Studio preload call shape must not raise.

    The second of the two ``api_mode``-passing call sites. Same keyword
    shape as :func:`test_update_model_accepts_host_call_shape_with_api_mode`
    — pinned separately so a future signature edit that breaks only one
    site is still caught.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine.update_model(
        model="local-model",
        context_length=32_768,
        base_url="http://localhost:1234/v1",
        api_key="",
        provider="lmstudio",
        api_mode="chat",
    )
    assert engine.context_length == 32_768
    assert engine.threshold_tokens == int(32_768 * engine.threshold_percent)


def test_update_model_five_param_init_path_still_works(tmp_home: Path) -> None:
    """The 5-param (no ``api_mode``) call shape still works.

    Five of run_agent.py's seven ``update_model`` call sites (e.g. the
    initial engine selection at run_agent.py:2301, the fallback
    activation at :8811) pass exactly the ABC's 5-parameter signature.
    The v0.1.1 override must not break that path — ``api_mode`` defaults
    to ``""`` and is ignored.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine.update_model(
        model="gpt-4o",
        context_length=128_000,
        base_url="https://api.openai.com/v1",
        api_key="sk-init",
        provider="openai",
    )
    assert engine.context_length == 128_000
    assert engine.threshold_tokens == int(128_000 * engine.threshold_percent)


def test_update_model_positional_minimal_call_works(tmp_home: Path) -> None:
    """The minimal 2-positional-arg call (``model``, ``context_length``) works.

    ``base_url`` / ``api_key`` / ``provider`` / ``api_mode`` all default,
    so a caller passing only the two required positionals must succeed.
    Guards the positional-call path alongside the keyword-call paths
    above.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    engine.update_model("some-model", 64_000)
    assert engine.context_length == 64_000
    assert engine.threshold_tokens == int(64_000 * engine.threshold_percent)


def test_update_model_recalculates_threshold_on_switch(tmp_home: Path) -> None:
    """A second ``update_model`` re-derives the budget for the new window.

    Mid-session model switch: the engine starts with one context window,
    then ``/model`` switches to a smaller one. ``threshold_tokens`` must
    track the NEW ``context_length``, not stay pinned to the old value —
    otherwise the compaction gate would fire against a stale budget.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())

    # First model — large context window.
    engine.update_model("big-model", 200_000, provider="anthropic", api_mode="chat")
    assert engine.context_length == 200_000
    big_threshold = engine.threshold_tokens
    assert big_threshold == int(200_000 * engine.threshold_percent)

    # Switch to a smaller-window model mid-session.
    engine.update_model("small-model", 8_192, provider="openai", api_mode="chat")
    assert engine.context_length == 8_192
    assert engine.threshold_tokens == int(8_192 * engine.threshold_percent)
    assert engine.threshold_tokens < big_threshold, (
        "threshold_tokens did not shrink with the smaller context window — "
        "update_model is not re-deriving the budget"
    )


def test_update_model_ignores_unknown_forward_compat_kwargs(tmp_home: Path) -> None:
    """Unknown keyword args are absorbed by ``**kwargs`` — no raise.

    The override carries a ``**kwargs`` sink so a future Hermes release
    that forwards an additional keyword to ``update_model`` degrades
    gracefully instead of crashing the turn (the same failure class as
    the original ``api_mode`` bug). Pin that forward-compat contract.
    """
    engine = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    # ``api_mode`` plus a hypothetical future keyword.
    engine.update_model(
        model="future-model",
        context_length=100_000,
        provider="anthropic",
        api_mode="responses",
        some_future_hermes_kwarg="whatever",
    )
    assert engine.context_length == 100_000
    assert engine.threshold_tokens == int(100_000 * engine.threshold_percent)
