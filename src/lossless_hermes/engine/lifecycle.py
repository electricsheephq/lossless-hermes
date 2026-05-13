"""Lifecycle methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the ``on_session_start`` / ``on_session_end`` / ``on_session_reset``
implementations per ADR-027 §Decision "Package structure" — the
``lifecycle.py`` sub-module of ``src/lossless_hermes/engine/``.

Per ADR-001 §Consequences "heavy init belongs in
``ContextEngine.on_session_start``", :meth:`on_session_start` is the
**first DB open attempt** in the engine lifecycle — the shell-class
:meth:`LCMEngine.__init__` deliberately does NOT touch the DB. The
Apple-system-Python ``enable_load_extension`` guard
(:func:`lossless_hermes.engine._check_sqlite_extension_loading`) is
therefore invoked here, immediately before :func:`open_lcm_db` (which
itself loads ``sqlite-vec`` via ``enable_load_extension``). Calling the
guard at construction time would reject Python installations that lack
extension-loading but never need to open the DB (e.g.
``actions/setup-python``'s pre-built CPython on macOS) — see the
discussion in ``src/lossless_hermes/engine/__init__.py`` module
docstring §"Apple system Python guard".

The 02-03 implementation focuses on the DB-bring-up cluster (open
connection, run migrations, instantiate the four Epic-01 stores) and
the symmetric tear-down on :meth:`on_session_end`. The richer
conversation-creation / ``_last_seen_message_idx`` initialization that
the full spec describes (per ``epics/02-engine-skeleton/
02-03-on-session-lifecycle.md``) is split across the rest of Epic 02:
this issue ships the heavy-init machinery, downstream Epic 02 issues
wire in the per-session bookkeeping.

Mixin contract (per ADR-027 §Consequences "All state lives on the shell
class"):

* No state owned here. The methods read/write ``self._db``,
  ``self._conversation_store`` etc. exclusively via the shell class's
  attributes declared in :meth:`LCMEngine.__init__`.
* No cross-mixin imports. If lifecycle work needs ingest behavior, it
  goes through ``self.ingest_batch(...)`` (MRO resolves to
  :class:`_IngestMixin`).
* Underscore-prefixed class name — not exported, only consumed by
  :class:`LCMEngine`.

See:

* ``docs/adr/001-plugin-distribution-model.md`` — entry-point contract,
  "heavy init in ``on_session_start``" mandate.
* ``docs/adr/002-plugin-data-directory.md`` — canonical
  ``$HERMES_HOME/lossless-hermes/lcm.db`` location.
* ``docs/adr/004-sqlite3-backend.md`` — Apple-system-Python guard
  policy ("before any DB open attempt").
* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/porting-guides/engine.md`` §"bootstrap" + §"maintain" — the
  TS algorithm that informs the full Epic-02 lifecycle body.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

# IMPORTANT — circular-import note: ``db.connection`` itself imports
# :func:`_check_sqlite_extension_loading` from ``lossless_hermes.engine``
# (the shell ``__init__``). Importing ``db.connection`` /
# ``db.migration`` / ``store.*`` at module load time here would form
# the cycle ``engine.__init__ -> lifecycle -> db.connection -> engine``
# during the package-init phase, so collection fails with
# ``ImportError: cannot import name '_check_sqlite_extension_loading'``.
# We defer those imports to the first :meth:`_LifecycleMixin.on_session_start`
# call, by which point the engine package has finished loading.
if TYPE_CHECKING:
    # Type-only imports for the mixin's reads/writes against shell state.
    # The real attribute creation happens in :meth:`LCMEngine.__init__`;
    # we re-declare here so ty can see what reads like
    # ``self._last_seen_message_idx.clear()`` are referring to.
    from lossless_hermes.db.config import LcmConfig
    from lossless_hermes.store.compaction_maintenance import CompactionMaintenanceStore
    from lossless_hermes.store.compaction_telemetry import CompactionTelemetryStore
    from lossless_hermes.store.conversation import ConversationStore
    from lossless_hermes.store.summary import SummaryStore


logger = logging.getLogger("lossless_hermes.engine.lifecycle")


def _resolve_db_path(engine: Any) -> Path:
    """Return the absolute path to ``lcm.db`` for ``engine``.

    Resolves per ADR-002 §"Option A: ``$HERMES_HOME/lossless-hermes/``":

    1. If ``engine.config.database_path`` is a non-empty string, use it
       verbatim (operators may override the canonical location via
       env / ``config.yaml``; the resolver in :mod:`db.config` already
       fills in the default for production callers).
    2. Otherwise, fall back to
       ``engine.hermes_home / "lossless-hermes" / "lcm.db"`` — the
       ADR-002 canonical path. This branch fires when the engine was
       constructed with a bare ``LcmConfig()`` (the test/no-args path),
       where ``database_path`` defaults to ``""`` per
       :class:`LcmConfig` §"Top-level scalars".

    Args:
        engine: The :class:`LCMEngine` instance (typed ``Any`` to avoid a
            circular import from the lifecycle mixin module back into
            the shell class).

    Returns:
        Absolute :class:`pathlib.Path` to the ``lcm.db`` file.
    """
    configured = (engine.config.database_path or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(engine.hermes_home) / "lossless-hermes" / "lcm.db"


class _LifecycleMixin:
    """Lifecycle hook handlers for :class:`LCMEngine`.

    Maps to ``engine.ts`` ``bootstrap()`` (lines 4983-5424),
    ``handleBeforeReset`` (line 7415), ``handleSessionEnd`` (line 7468).
    The Python port drops the JSONL fast-paths per ADR-011 (Hermes has
    no transcript file; sessions begin fresh).
    """

    # ------------------------------------------------------------------
    # Type-checker stubs (TYPE_CHECKING-only) declaring the shell state
    # this mixin reads/writes. Per ADR-027 §Consequences "All state lives
    # on the shell class", the real attribute creation happens in
    # :meth:`LCMEngine.__init__`. We re-declare the types here (gated on
    # :data:`TYPE_CHECKING` so the runtime class body stays clean) so ty
    # can resolve reads like ``self._last_seen_message_idx.clear()``
    # against a known type. Mirrors the pattern other Epic-02 mixins
    # adopt as they fill in bodies.
    # ------------------------------------------------------------------
    if TYPE_CHECKING:
        _db: Optional[sqlite3.Connection]
        _conversation_store: Optional[ConversationStore]
        _summary_store: Optional[SummaryStore]
        _telemetry_store: Optional[CompactionTelemetryStore]
        _maintenance_store: Optional[CompactionMaintenanceStore]
        _last_seen_message_idx: Dict[str, int]
        hermes_home: Path
        config: LcmConfig
        last_prompt_tokens: int
        last_completion_tokens: int
        last_total_tokens: int
        compression_count: int

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """Open ``lcm.db``, run migrations, and instantiate the four stores.

        Per ADR-001 §Consequences "heavy init belongs in
        ``ContextEngine.on_session_start``", this is the **first DB
        open attempt** in the engine lifecycle. Per ADR-004
        §Consequences "Apple system Python guard", we invoke
        :func:`_check_sqlite_extension_loading` before the
        :func:`open_lcm_db` call so the failure surface is one clean
        :class:`RuntimeError` with an actionable install hint, not an
        :class:`AttributeError` deep inside ``sqlite_vec.load``.

        Idempotent across repeated calls on the same engine instance:
        when ``self._db`` is already open, the method is a no-op
        (subsequent ``on_session_start`` calls for different
        ``session_id``s share the connection — per-session state lives
        elsewhere in ``self._last_seen_message_idx`` / etc.).

        Maps to engine.ts ``bootstrap()`` (lines 4983-5424). The
        JSONL fast-paths all drop per ADR-011 — there is no transcript
        file to checkpoint against.

        Args:
            session_id: The Hermes session identifier. Used for log
                context; per-session bookkeeping (conversation row,
                ``_last_seen_message_idx``) is initialized by the
                downstream Epic-02 issues.
            **kwargs: Per ``docs/reference/hermes-hooks.md``: may
                include ``hermes_home``, ``platform``, ``model``,
                ``context_length``. The 02-03 body does not consume
                these — downstream Epic-02 issues wire them in.

        Raises:
            RuntimeError: Apple's system Python lacks
                ``sqlite3.Connection.enable_load_extension`` (the
                guard fires with the documented install hint).
        """
        # Per-instance idempotence: an engine that has already opened
        # the DB stays open. Multiple ``on_session_start`` calls for
        # different session_ids are common (CLI restart on the same
        # process), and re-opening would (a) lose any test-fixture
        # state, (b) churn the WAL on file-backed DBs, (c) re-run the
        # migration ladder unnecessarily (it's idempotent but the
        # ``BEGIN EXCLUSIVE`` walk is non-trivial).
        if self._db is not None:
            logger.debug(
                "[lcm] on_session_start session_id=%s: DB already open, no-op",
                session_id,
            )
            return

        # Apple-system-Python guard fires BEFORE the first DB-open
        # attempt. The guard helper is owned by the shell module so
        # tests and storage.py see one error message. Imports here are
        # deferred to avoid the lifecycle <-> db.connection <-> engine
        # circular import (see the module-level note).
        from lossless_hermes.db.connection import open_lcm_db
        from lossless_hermes.db.features import get_lcm_db_features
        from lossless_hermes.db.migration import run_lcm_migrations
        from lossless_hermes.engine import _check_sqlite_extension_loading
        from lossless_hermes.store.compaction_maintenance import (
            CompactionMaintenanceStore,
        )
        from lossless_hermes.store.compaction_telemetry import (
            CompactionTelemetryStore,
        )
        from lossless_hermes.store.conversation import ConversationStore
        from lossless_hermes.store.summary import SummaryStore

        _check_sqlite_extension_loading()

        db_path = _resolve_db_path(self)
        logger.debug(
            "[lcm] on_session_start session_id=%s: opening DB at %s",
            session_id,
            db_path,
        )
        conn = open_lcm_db(db_path)

        # Probe FTS5 availability AFTER the connection is open but
        # BEFORE migrations run, so the migration ladder skips FTS5
        # branches on builds that lack the extension. The flag also
        # flows into the stores so reads route to the LIKE-fallback
        # paths uniformly.
        features = get_lcm_db_features(conn)

        try:
            run_lcm_migrations(conn, fts5_available=features.fts5_available)
        except Exception:
            # If migrations fail, close the half-initialized connection
            # before re-raising so callers don't see a stale ``self._db``.
            from lossless_hermes.db.connection import close_lcm_db

            close_lcm_db(conn)
            raise

        # Wire state onto the shell class. The four stores share the
        # one connection — ADR-017 §"Synchronous by design" means each
        # store is a thin CRUD wrapper, not a lifecycle owner.
        self._db = conn
        self._conversation_store = ConversationStore(
            conn,
            fts5_available=features.fts5_available,
        )
        self._summary_store = SummaryStore(
            conn,
            fts5_available=features.fts5_available,
            trigram_tokenizer_available=features.fts5_trigram_available,
        )
        self._telemetry_store = CompactionTelemetryStore(conn)
        self._maintenance_store = CompactionMaintenanceStore(conn)

        logger.info(
            "[lcm] on_session_start session_id=%s: DB open at %s (fts5=%s, vec0=%s)",
            session_id,
            db_path,
            features.fts5_available,
            features.vec0_available,
        )

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Flush pending state, close the DB, clear store references.

        Maps to engine.ts ``handleSessionEnd`` (line 7468). Fires at
        REAL session boundaries (CLI exit, ``/reset``, gateway expiry —
        per ``docs/reference/hermes-hooks.md``), NOT per-turn.

        Idempotent — safe to call when the DB is already closed (e.g.
        Hermes invoking ``on_session_end`` after a previous
        ``on_session_end`` already tore things down on process exit).

        Args:
            session_id: The Hermes session identifier. Used for log
                context.
            messages: The final conversation message list. The 02-03
                body does not consume this — downstream Epic-03 issues
                add the defense-in-depth tail-ingest for interrupted
                turns (ADR-009 §Consequences).
        """
        if self._db is None:
            logger.debug(
                "[lcm] on_session_end session_id=%s: DB already closed, no-op",
                session_id,
            )
            return

        logger.debug(
            "[lcm] on_session_end session_id=%s: closing DB (messages=%d)",
            session_id,
            len(messages) if messages else 0,
        )

        # Close the connection through the sanctioned factory so the
        # registry stays consistent (test fixtures rely on
        # ``close_lcm_connection(path=...)`` finding zero tracked
        # connections after teardown). Deferred import — see module
        # docstring on the circular-import seam.
        from lossless_hermes.db.connection import close_lcm_db

        close_lcm_db(self._db)

        # Clear every store reference so a stale post-close call into
        # ``self._conversation_store.<method>`` surfaces as a clear
        # ``AttributeError`` (or the per-method ``RuntimeError`` guard
        # that downstream issues add), not a use-after-close on a
        # silently-broken sqlite3.Connection.
        self._db = None
        self._conversation_store = None
        self._summary_store = None
        self._telemetry_store = None
        self._maintenance_store = None

    def on_session_reset(self) -> None:
        """Reset per-session state. Does NOT close the DB.

        Maps to engine.ts ``handleBeforeReset`` (line 7415). The default
        ABC implementation zeroes the four token-state fields
        (``last_prompt_tokens`` / ``last_completion_tokens`` /
        ``last_total_tokens`` / ``compression_count``) — we chain through
        :func:`super().on_session_reset` so that contract still holds.

        Beyond the ABC default, this body clears the diff-ingest cursor
        (``_last_seen_message_idx``) for every session the engine has
        seen this process. The DB connection and stores stay open —
        ``/reset`` is a within-process operation, not a teardown.
        """
        # Default ABC behavior — zero token counters. The real Hermes
        # ABC (``agent.context_engine.ContextEngine``) provides a
        # concrete ``on_session_reset`` that zeroes ``last_*_tokens`` +
        # ``compression_count``; the Hermes-less bridge stub has no
        # such method (it is a synthesized class with only ``__init__``
        # — see ``hermes_bridge.py`` lines 63-64). To stay robust in
        # both envs we look the parent method up via ``super()`` and
        # zero the counters ourselves when it is absent (the bridge
        # stub case). Replicates the ABC body so the invariant holds
        # uniformly.
        parent_reset = getattr(super(), "on_session_reset", None)
        if callable(parent_reset):
            parent_reset()
        else:
            # Hermes-less env: inline the ABC default body.
            self.last_prompt_tokens = 0
            self.last_completion_tokens = 0
            self.last_total_tokens = 0
            self.compression_count = 0

        # LCM-specific: clear per-session diff-ingest cursors. The
        # full handleBeforeReset additionally archives the conversation
        # row, but that has dependencies on ``ConversationStore.archive``
        # methods that the plugin-glue hook (issue 02-07) wires in.
        # Here we keep to the symmetric reset of state owned by the
        # shell at 02-01.
        self._last_seen_message_idx.clear()

        logger.debug("[lcm] on_session_reset: token state + ingest cursors cleared")
