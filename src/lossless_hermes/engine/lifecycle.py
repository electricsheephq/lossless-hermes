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
        _ingest_cursor_reconciled: set[str]
        _previous_assembled_messages_by_conversation: Dict[int, Any]
        current_session_id: Optional[str]
        hermes_home: Path
        config: LcmConfig
        last_prompt_tokens: int
        last_completion_tokens: int
        last_total_tokens: int
        compression_count: int
        # Compaction-budget state — written by :meth:`update_model`'s
        # Hermes-less fallback branch (the ABC default owns the
        # Hermes-available path). ``threshold_percent`` is a class
        # attribute on the :class:`LCMEngine` shell; declared here so ty
        # can resolve the ``self.threshold_percent`` read in that branch.
        context_length: int
        threshold_tokens: int
        threshold_percent: float

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
        #
        # Always update ``current_session_id`` though — even on the
        # re-entrant DB-already-open path. Subsequent ``on_session_start``
        # calls reflect a new Hermes session (CLI restart on the same
        # process), and Epic 08's ``/lcm status`` resolves the current
        # conversation off this field. Per ``docs/porting-guides/
        # plugin-glue.md`` §"Per-subcommand translation table" line 650,
        # this field replaces the TS ``ctx.sessionId``.
        self.current_session_id = session_id or None
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

        # Clear ``current_session_id`` symmetrically with the DB close.
        # Epic 08 ``/lcm status`` post-close should report "no active
        # conversation" (the same shape pre-first-``on_session_start``),
        # not the stale prior session_id from before tear-down.
        self.current_session_id = None

    def _on_session_end_hook(
        self,
        session_id: str = "",
        completed: bool = True,
        interrupted: bool = False,
        model: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> None:
        """Hermes ``on_session_end`` **plugin hook** — per-turn cadence (ADR-009).

        Wired into :func:`lossless_hermes.register` as
        ``ctx.register_hook("on_session_end", engine._on_session_end_hook)``.
        Distinct from the :class:`ContextEngine` ABC method
        :meth:`on_session_end` (which fires at real session boundaries:
        ``shutdown_memory_provider``, ``commit_memory_session`` —
        ``run_agent.py:5575,5600``); this plugin hook fires at the end
        of **every** ``run_conversation`` call (``run_agent.py:15525``)
        — i.e., once per user turn — plus a safety-net fire on
        interrupted CLI exit (``cli.py:13233``). See
        ``docs/reference/hermes-hooks.md`` line 96 for the
        documentation of this distinction.

        Per ADR-009 §Consequences "defense-in-depth for interrupted
        turns", the role of this hook is to catch any ``post_llm_call``
        that did NOT fire (``run_agent.py:15407`` gates on
        ``final_response and not interrupted``). At 02-07 the body is a
        no-op debug-log stub — Epic 03 fills in the tail-ingest path
        that calls :meth:`_IngestMixin._on_post_llm_call` for any
        message that landed in ``conversation_history`` after the last
        successful ingest cursor.

        Note that the **kwargs shape here is the PLUGIN-HOOK shape**
        (``session_id``, ``completed``, ``interrupted``, ``model``,
        ``platform``) — NOT the ABC :meth:`on_session_end` signature
        (which takes ``messages: List[Dict]``). The plugin hook does
        not receive the final message list; if Epic 03's tail-ingest
        needs the messages it must read them from the engine's own
        :attr:`_conversation_store` keyed by ``session_id``.

        Args:
            session_id: The Hermes session identifier.
            completed: Whether ``run_conversation`` ran to a clean
                completion (final response set, no exception).
            interrupted: Whether the turn was interrupted (Ctrl-C in
                CLI, gateway client disconnect, etc.). The Epic-03
                tail-ingest body fires only when this is ``True``.
            model: The LLM model id.
            platform: The provider platform string.
            **kwargs: Forward-compat for future hook signature
                additions.
        """
        # No-op stub. Epic 03 replaces this body with the real defense-
        # in-depth tail-ingest path (catch up any messages the
        # ``post_llm_call`` hook didn't see because ``interrupted``
        # short-circuited the dispatch). The debug log gives an
        # operator scanning logs a breadcrumb that the hook fired.
        if interrupted:
            logger.debug(
                "[lcm] on_session_end (plugin hook) session=%s interrupted=True "
                "(Epic 03 will catch up on tail messages)",
                session_id,
            )
        else:
            logger.debug(
                "[lcm] on_session_end (plugin hook) session=%s completed=%s "
                "(per-turn cadence — ABC on_session_end handles real boundaries)",
                session_id,
                completed,
            )

    def _on_subagent_stop(
        self,
        parent_session_id: str = "",
        child_role: Any = None,
        child_summary: str = "",
        child_status: str = "",
        duration_ms: int = 0,
        **kwargs: Any,
    ) -> None:
        """Hermes ``subagent_stop`` hook — no-op stub (ADR-012 defers to v2).

        Wired into :func:`lossless_hermes.register` as
        ``ctx.register_hook("subagent_stop", engine._on_subagent_stop)``.
        Fires once per child after ``delegate_task`` runs (per
        ``tools/delegate_tool.py:2248``), serialised on the parent
        thread so plugin authors don't have to handle concurrency.

        Per ADR-012 §Decision, v1 of lossless-hermes does NOT share
        subagent context with the parent — each subagent runs its own
        full ``run_conversation`` with its own ``pre_llm_call`` /
        ``post_llm_call`` hooks (per ``docs/reference/hermes-hooks.md``
        §"Subagent / delegate_task lifecycle"). Epic 06 wires the real
        subagent-context-sharing behavior (v2). Until then the hook is
        registered as a forward-compat seam — registering at 02-07
        means an Epic-06 patch only has to fill the body, not edit
        :func:`register`.

        Per ``docs/reference/hermes-hooks.md`` line 99, the kwargs
        shape is ``parent_session_id``, ``child_role`` (Any),
        ``child_summary``, ``child_status``, ``duration_ms``. Every
        kwarg is accepted (defaults provided + ``**kwargs`` catches
        forward-compat additions) and no exception fires.

        Args:
            parent_session_id: The parent agent's session identifier.
            child_role: The child agent's role (typed ``Any`` because
                Hermes may pass an enum, a string, or a dict).
            child_summary: The child's final summary text.
            child_status: The child's exit status (``"completed"``,
                ``"error"``, ``"timeout"``, etc.).
            duration_ms: Wall-clock duration of the child's run.
            **kwargs: Forward-compat for future hook signature
                additions.
        """
        # No-op stub. Epic 06 replaces this body with the real
        # subagent-context-sharing path (v2 of ADR-012). The debug log
        # gives an operator scanning logs a breadcrumb that the hook
        # fired so the integration test can confirm it's wired.
        logger.debug(
            "[lcm] subagent_stop parent=%s status=%s duration_ms=%d "
            "(v1: no-op per ADR-012; Epic 06 wires real behavior)",
            parent_session_id,
            child_status,
            duration_ms,
        )

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

        # v0.1.2 fix (issue #130): clear the restart-reconciliation
        # tracking set in lockstep with the cursor dict. ``/reset``
        # starts a fresh conversation, so the next ingest for any
        # session_id must re-run :meth:`_IngestMixin._reconcile_ingest_cursor`
        # against the (now-archived-or-empty) durable store rather than
        # trusting a stale "already reconciled" mark.
        self._ingest_cursor_reconciled.clear()

        logger.debug("[lcm] on_session_reset: token state + ingest cursors cleared")

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
        **kwargs: Any,
    ) -> None:
        """Recalculate context budget on a mid-session model switch.

        Hermes calls ``context_compressor.update_model(...)`` at seven
        sites in ``run_agent.py``. Five of them pass the five-argument
        shape the :class:`~agent.context_engine.ContextEngine` ABC
        declares (``model``, ``context_length``, ``base_url``,
        ``api_key``, ``provider`` — ``context_engine.py:191``). The
        remaining **two** pass an extra ``api_mode=`` keyword:

        * ``run_agent.py:2587`` — the LM-Studio context preload, after a
          ``/model`` switch resolves the newly-loaded context window.
        * ``run_agent.py:2728`` — the in-place model switch
          (``switch_model`` → ``update_model``).

        Both of those call sites pass **every** argument by keyword,
        including ``api_mode=self.api_mode``. The ABC's default
        ``update_model`` has no ``api_mode`` parameter, and
        :class:`LCMEngine` does not otherwise override the method — so
        without this shim a ``/model`` switch raises
        ``TypeError: update_model() got an unexpected keyword argument
        'api_mode'`` and crashes the turn.

        This override absorbs ``api_mode`` (and any future keyword the
        host may start forwarding, via ``**kwargs``) and delegates the
        real work to :func:`super().update_model`, so the ABC default
        still recalculates :attr:`context_length` and
        :attr:`threshold_tokens` from :attr:`threshold_percent`. LCM has
        no per-``api_mode`` budget logic — the parameter is accepted and
        ignored purely so the host call shape never raises.

        Hermes-less robustness: like :meth:`on_session_reset`, this body
        looks the parent method up via ``super()`` and falls back to
        inlining the ABC default's two-line budget recalculation when it
        is absent. The Hermes-less :mod:`hermes_bridge` stub
        ``ContextEngine`` is a synthesized :class:`type` with only
        ``__init__`` (``hermes_bridge.py:64``) — it carries no
        ``update_model`` — so a bare ``super().update_model(...)`` would
        raise :class:`AttributeError` under the test suite (Hermes is
        intentionally not a pip dependency per ADR-007).

        .. note::

            ``api_mode`` is **deliberately unused**. It is on the
            signature solely to match the host call shape at
            ``run_agent.py:2587`` / ``:2728``. If a future LCM revision
            grows ``api_mode``-dependent budget behaviour, this is the
            seam to implement it; until then "accept and ignore" is the
            correct, forward-compatible contract.

        Args:
            model: The new model identifier.
            context_length: The new model's context window, in tokens.
            base_url: Provider base URL (forwarded to the ABC default).
            api_key: Provider API key (forwarded to the ABC default).
            provider: Provider name (forwarded to the ABC default).
            api_mode: The host's API-mode string (e.g. from
                ``run_agent.py``'s ``self.api_mode``). Accepted to match
                the host call shape; not consumed by LCM.
            **kwargs: Forward-compat sink for any additional keyword the
                host may forward to ``update_model`` in future Hermes
                releases. Accepted and ignored.
        """
        # Delegate to the ABC default (five-arg signature). It updates
        # ``self.context_length`` and re-derives ``self.threshold_tokens``
        # from ``self.threshold_percent`` — the contract every other
        # ``update_model`` call site already relies on. ``api_mode`` and
        # ``**kwargs`` are intentionally NOT forwarded: the ABC default
        # does not accept them, and LCM has no use for them.
        #
        # Hermes-less env: the bridge stub ``ContextEngine`` has no
        # ``update_model`` (it is a synthesized ``type`` with only
        # ``__init__`` — ``hermes_bridge.py:64``), so look the parent up
        # via ``super()`` and inline the ABC's two-line body when it is
        # absent. Same defensive pattern as ``on_session_reset`` above.
        parent_update = getattr(super(), "update_model", None)
        if callable(parent_update):
            parent_update(model, context_length, base_url, api_key, provider)
        else:
            # Hermes-less env: inline the ABC default body
            # (agent/context_engine.py:205-206).
            self.context_length = context_length
            self.threshold_tokens = int(context_length * self.threshold_percent)
        logger.debug(
            "[lcm] update_model: model=%s context_length=%d (api_mode=%r ignored)",
            model,
            context_length,
            api_mode,
        )

    # ------------------------------------------------------------------
    # Issue 08-16 — ``/lcm rotate`` engine support.
    #
    # Per ADR-024 §"Consequences" + Epic 01 README ("JSONL bootstrap,
    # file-anchor checkpointing, session-file rollover" drop entirely),
    # ``/lcm rotate`` in Hermes is SQLite-only — there is no JSONL
    # transcript file to rename. The TS source's
    # ``rotateSessionStorageWithBackup`` (``lossless-claw/src/engine.ts``)
    # physically rotated the session JSONL; the Hermes equivalent backs
    # up the DB, clears the per-session assemble snapshot cache, compacts
    # the WAL, and stamps ``state_meta.last_rotate_at``. The backup +
    # WAL-compaction live in :mod:`lossless_hermes.commands.rotate`; the
    # two state-touching primitives below are engine-owned because they
    # mutate engine-internal state (the snapshot dict) and the DB.
    # ------------------------------------------------------------------

    def clear_assemble_snapshot(self, session_id: str) -> bool:
        """Drop the assemble prefix-stability snapshot for ``session_id``.

        Removes the per-conversation entry from
        :attr:`_previous_assembled_messages_by_conversation` so the next
        :meth:`_AssembleMixin._assemble` pass for that conversation
        rebuilds from scratch instead of comparing against the cached
        prior assembly (the prefix-stability diagnostic snapshot —
        see the field doc in :meth:`LCMEngine.__init__` and the snapshot
        write in ``assemble.py``).

        Called by ``/lcm rotate`` (issue 08-16, step 2). The snapshot
        dict is keyed by ``conversation_id`` (an ``int``), NOT by
        ``session_id`` — so this method first resolves the session's
        conversation row via
        :meth:`ConversationStore.get_conversation_by_session_id` and then
        deletes the keyed entry. This mirrors how
        :meth:`_AssembleMixin._assemble_locked` resolves the same id.

        Best-effort and never raises:

        * No DB / no conversation store (pre-``on_session_start``) —
          returns :data:`False`, no-op.
        * Session has no conversation row yet — returns :data:`False`,
          no-op (nothing to clear; the next assemble would seed a fresh
          entry anyway).
        * Conversation resolved but no snapshot cached for it — returns
          :data:`False` (the ``dict.pop`` default branch).

        Args:
            session_id: The Hermes session identifier whose conversation
                snapshot should be dropped. Typically
                :attr:`current_session_id`.

        Returns:
            :data:`True` if a snapshot entry was actually removed;
            :data:`False` if there was nothing to remove (no
            conversation, or no cached snapshot for it).
        """
        store = self._conversation_store
        if store is None:
            logger.debug(
                "[lcm] clear_assemble_snapshot session=%s: no conversation "
                "store (pre-on_session_start) — no-op",
                session_id,
            )
            return False

        conversation = store.get_conversation_by_session_id(session_id)
        if conversation is None:
            logger.debug(
                "[lcm] clear_assemble_snapshot session=%s: no conversation row — no-op",
                session_id,
            )
            return False

        conversation_id = conversation.conversation_id
        # ``dict.pop`` with a sentinel default is the atomic
        # remove-if-present idiom — no separate membership check (which
        # would be a TOCTOU window). The return value tells the caller
        # whether anything was actually cleared.
        sentinel = object()
        removed = (
            self._previous_assembled_messages_by_conversation.pop(conversation_id, sentinel)
            is not sentinel
        )
        logger.debug(
            "[lcm] clear_assemble_snapshot session=%s conversation_id=%d: snapshot %s",
            session_id,
            conversation_id,
            "cleared" if removed else "absent (no-op)",
        )
        return removed

    def write_state_meta(self, key: str, value: str) -> None:
        """UPSERT a ``(key, value)`` row into the ``state_meta`` table.

        ``state_meta`` is a Hermes-only single-row-per-key store
        (``key TEXT PRIMARY KEY, value TEXT, updated_at TEXT``). It is
        deliberately NOT in the migration ladder — per
        ``src/lossless_hermes/cli/import_openclaw.py`` §"``state_meta``
        design note", a Python-only table in ``db/migration.py`` would
        trip the schema-diff CI gate (the gate exits non-zero on objects
        absent from the TS reference schema). The table is therefore
        created on demand by whichever caller writes first; this method
        runs the same ``CREATE TABLE IF NOT EXISTS`` so ``/lcm rotate``
        works on a DB that has never been through ``import-openclaw``.

        Called by ``/lcm rotate`` (issue 08-16, step 4) to stamp
        ``last_rotate_at`` so ``/lcm status`` can later show "last
        rotated N ago".

        Per [ADR-017](../../docs/adr/017-sync-vs-async-db.md) this is a
        synchronous DB write — it commits before returning so the row is
        durable even if the process exits immediately after ``/lcm
        rotate``.

        Args:
            key: The ``state_meta`` key (e.g. ``"last_rotate_at"``).
            value: The string value to store (callers serialize
                timestamps as ISO-8601 UTC strings).

        Raises:
            RuntimeError: The engine has no open DB connection
                (``on_session_start`` has not run). Callers in Epic 08
                resolve the DB defensively before reaching this method,
                so this is a programmer-error guard rather than an
                expected path.
        """
        db = self._db
        if db is None:
            raise RuntimeError(
                "write_state_meta requires an open DB connection — on_session_start has not run."
            )

        # Create-on-demand: ``state_meta`` is not in the migration
        # ladder (see docstring). The DDL is byte-identical to
        # ``import_openclaw._ensure_state_meta_table`` so the two
        # writers agree on the schema.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS state_meta (
              key TEXT PRIMARY KEY,
              value TEXT,
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        db.execute(
            """
            INSERT INTO state_meta (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value),
        )
        db.commit()
        logger.debug("[lcm] write_state_meta: %s = %s", key, value)
