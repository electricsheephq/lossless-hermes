"""LCMEngine — Lossless Context Management engine shell.

This package hosts the :class:`LCMEngine` class shell that composes four
mixins per ADR-027 §Decision "Package structure":

* :class:`_LifecycleMixin` (``lifecycle.py``) — ``on_session_start`` /
  ``on_session_end`` / ``on_session_reset``.
* :class:`_CompactMixin` (``compact.py``) — ``compress`` overflow-recovery
  + state-machine helpers.
* :class:`_AssembleMixin` (``assemble.py``) — per-turn assembly
  substitution.
* :class:`_IngestMixin` (``ingest.py``) — ``post_llm_call`` diff-ingest.

Per ADR-027 §Consequences:

* **All state lives on this shell class.** Mixin methods read/write
  ``self._db``, ``self._conversation_store``, ``self._session_locks``
  etc. — every state attribute is declared in :meth:`__init__` here.
* **No mixin owns state.** Mixin files contain methods only; the shell
  class is the single state-creation site.
* **No cross-mixin imports.** Mixin methods that need behavior owned
  by a sibling mixin call ``self.<method>`` — MRO resolves to the
  appropriate :class:`_FooMixin` body.

At **issue 02-01** this file is the wired-but-skeleton shell:

* The four mixins are imported and composed in :class:`LCMEngine`'s
  bases tuple. Their bodies are stubs (mostly :class:`NotImplementedError`)
  that subsequent Epic 02–04 issues fill in.
* :meth:`__init__` instantiates state fields. Following the 00-06
  invariant (and ADR-001 §Consequences "heavy init belongs in
  ``ContextEngine.on_session_start``"), the constructor does NOT open
  the SQLite DB or run migrations — those land in 02-03
  (``on_session_start``). Store attributes default to ``None``; 02-03
  populates them.
* :meth:`compress` and :meth:`should_compress` retain the 00-06 no-op
  bodies via :class:`_CompactMixin` (passthrough; ``False``). Epic 04
  replaces those with the real compaction algorithm.

### Apple system Python guard

Per ADR-004 §Consequences, sqlite-vec loading requires
``sqlite3.Connection.enable_load_extension``, which Apple's system
``/usr/bin/python3`` and some pre-built CPython distributions (notably
``actions/setup-python``'s macOS builds) ship without.

The guard helper :func:`_check_sqlite_extension_loading` is deliberately
**not** wired into :meth:`LCMEngine.__init__` — there is no DB open
attempt in ``__init__`` (heavy init defers to :meth:`on_session_start`
per ADR-001 §Consequences). Firing the guard at construction time would
block legitimate Python installations from importing the package even
though no DB ever opens. Instead, the guard is exposed for the future
:meth:`on_session_start` body (issue 02-03) to call before its first
``open_lcm_db()`` invocation.

See:

* ``docs/adr/001-plugin-distribution-model.md`` — entry-point contract.
* ``docs/adr/004-sqlite-vec-distribution.md`` — sqlite extension guard.
* ``docs/adr/007-hermes-as-dependency.md`` — Hermes-less env story.
* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/porting-guides/engine.md`` — full engine port plan.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.hermes_bridge import ContextEngine
from lossless_hermes.store.compaction_maintenance import CompactionMaintenanceStore
from lossless_hermes.store.compaction_telemetry import CompactionTelemetryStore
from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.store.summary import SummaryStore

from .assemble import _AssembleMixin
from .circuit_breaker import CircuitBreaker
from .compact import _CompactMixin
from .ingest import _IngestMixin
from .lifecycle import _LifecycleMixin
from .session_locks import SessionLockRegistry

__all__ = [
    "APPLE_SYSTEM_PYTHON_MSG",
    "CircuitBreaker",
    "ContextEngineInfo",
    "LCMEngine",
]

logger = logging.getLogger("lossless_hermes.engine")


# ---------------------------------------------------------------------------
# Apple system Python guard (ADR-004 §Consequences)
# ---------------------------------------------------------------------------

APPLE_SYSTEM_PYTHON_MSG = (
    "lossless-hermes requires a Python build with sqlite3 extension "
    "loading enabled (sqlite3.Connection.enable_load_extension). Apple's "
    "system /usr/bin/python3 ships without this. Install Homebrew Python "
    "(`brew install python`), pyenv-managed Python, or a uv-managed "
    "Python and reinstall lossless-hermes into that interpreter."
)


def _has_sqlite_extension_loading() -> bool:
    """Return ``True`` if ``sqlite3.Connection.enable_load_extension`` is present.

    Factored out so tests can monkey-patch this single function — the
    underlying ``sqlite3.Connection`` type is C-immutable and cannot be
    altered directly via :meth:`pytest.MonkeyPatch.delattr`. By moving
    the introspection here, the test surface is one stable hook.
    """
    return hasattr(sqlite3.Connection, "enable_load_extension")


def _check_sqlite_extension_loading() -> None:
    """Raise :class:`RuntimeError` if ``enable_load_extension`` is missing.

    Apple's system Python compiles sqlite3 without ``--enable-loadable-
    extensions``, so ``sqlite3.Connection.enable_load_extension`` is absent.
    sqlite-vec (and any other sqlite extension) cannot load. The guard
    fires at construction time so the failure mode is "clear error at
    plugin-load", not "obscure error mid-session".

    See ADR-004 §Consequences "Apple system Python guard".
    """
    if not _has_sqlite_extension_loading():
        raise RuntimeError(APPLE_SYSTEM_PYTHON_MSG)


# ---------------------------------------------------------------------------
# ContextEngineInfo — identity record (per 02-02 spec)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextEngineInfo:
    """Identity + capability record for the engine.

    Per ``epics/02-engine-skeleton/02-02-engine-state.md`` table, this
    surface lives on :attr:`LCMEngine.info`. ``owns_compaction`` is
    ``True`` by default and degrades to ``False`` only if migration
    fails — Hermes consults it (post Epic 03/04 hook-up) to decide
    whether to defer compaction to the plugin or fall back to the
    in-host default. Issue 02-02 declares the field with the default
    record; Epic 04's compaction-failure handling can swap to a
    ``replace(self.info, owns_compaction=False)`` if needed.

    Frozen + slots: identity records are immutable (Hermes would not
    re-read after registration anyway) and the slots optimization
    keeps memory flat — many short-lived engine instances are created
    in tests.

    Maps to TS ``LcmContextEngine.info: ContextEngineInfo`` field
    declared near the top of ``src/engine.ts``.

    Attributes:
        name: Engine selector string. Matches ``context.engine: lcm``
            in ``~/.hermes/config.yaml`` (ADR-001 §Consequences).
        version: Semantic version of the engine.
        owns_compaction: Whether the engine drives its own compaction
            (vs. falling back to Hermes's in-host default). True at
            construction; degrades to False if migrations fail (per
            spec table row 1, "true unless migration failed"). The
            Epic-04 compaction-failure handler is the only writer.
    """

    name: str = "lcm"
    version: str = "0.1.0"
    owns_compaction: bool = True


# ---------------------------------------------------------------------------
# LCMEngine — shell class composing the four mixins
# ---------------------------------------------------------------------------


class LCMEngine(_LifecycleMixin, _CompactMixin, _AssembleMixin, _IngestMixin, ContextEngine):
    """Lossless Context Management engine for Hermes.

    Per ADR-027 §Decision, this is the **shell class** that composes
    four mixins (lifecycle / compact / assemble / ingest). Every state
    field is owned by this class (declared in :meth:`__init__`); mixin
    methods exclusively read/write ``self.<state>``.

    MRO (Python C3 linearization, per ADR-027 §Decision):

    ``LCMEngine -> _LifecycleMixin -> _CompactMixin -> _AssembleMixin
    -> _IngestMixin -> ContextEngine -> object``

    At issue 02-01 this is a skeleton: stores are ``None`` (instantiated
    in 02-03's ``on_session_start``), the ingest/assemble/compact bodies
    in the mixins are stubs (filled in Epic 03/04). The class still
    satisfies the ABC contract — :meth:`compress` is a passthrough and
    :meth:`should_compress` returns ``False``, both via :class:`_CompactMixin`.

    Maps to ``lossless-claw/src/engine.ts`` ``LcmContextEngine`` class
    declaration (line 1739) + constructor (lines 1808-1900).

    Attributes:
        hermes_home: Path to ``$HERMES_HOME`` (typically ``~/.hermes/``).
            Per ADR-001, the engine takes the Hermes-side path as a
            constructor arg rather than computing it itself, so tests
            can substitute a ``tmp_path``.
        config: Validated :class:`LcmConfig` instance.

    The ``name`` class attribute is the canonical engine selector — it
    matches ``context.engine: lcm`` in ``~/.hermes/config.yaml`` per
    ADR-001 §Consequences. The string ``"lcm"`` is the entry-point
    binding the Hermes plugin-selection ladder keys off.
    """

    # ABC §Identity ----------------------------------------------------------
    # ``name`` is declared as ``@property @abstractmethod`` on the ABC, but
    # a class attribute satisfies the abstract requirement in Python (the
    # name is present on the class, which is all ``__init_subclass__``
    # checks for). Confirmed by Hermes's own ``ContextCompressor.name`` as
    # a ``@property`` and the test stub in
    # ``tests/agent/test_context_engine.py`` line 25 that uses a property —
    # both work. Class attribute is the idiomatic choice when constant.
    name: str = "lcm"

    # ABC §Compaction parameters (inherited, can be overridden later) -------
    # Keeping defaults from the ABC for 02-01. Epic 02 fill-in issues
    # override these from ``self.config`` once the config wiring lands.
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 8  # LCM standard, override of ABC's default 6

    def __init__(
        self,
        hermes_home: Optional[Path] = None,
        config: Optional[LcmConfig] = None,
    ) -> None:
        """Initialize the engine shell.

        Per ADR-001 §Consequences "heavy init belongs in
        ``ContextEngine.on_session_start``": no DB open, no migration
        run, no background task. Store attributes default to ``None``;
        :meth:`on_session_start` (issue 02-03) opens the DB and
        instantiates them.

        Args:
            hermes_home: Path to ``$HERMES_HOME``. Defaults to ``None``;
                callers (specifically :func:`lossless_hermes.register`)
                pass the resolved path from
                :func:`lossless_hermes.hermes_bridge.get_hermes_home`.
            config: Validated :class:`LcmConfig`. Defaults to ``None``,
                in which case an empty :class:`LcmConfig` is constructed.

        Note:
            We deliberately do **not** call ``super().__init__()``. In a
            Hermes-less env (no ``agent.context_engine`` importable) the
            ``hermes_bridge`` re-exports a stub ``ContextEngine`` whose
            ``__init__`` raises :class:`LosslessHermesEnvironmentError`.
            The ABC itself has no required ``__init__`` (every test stub
            in Hermes's own suite — ``tests/agent/test_context_engine.py``
            line 18 — defines its own ``__init__`` without chaining). So
            skipping ``super().__init__()`` is correct in both Hermes-
            available and Hermes-less envs.

        Note:
            The Apple-Python sqlite-extension-loading guard
            (:func:`_check_sqlite_extension_loading`) is **not** invoked
            here. There is no DB open attempt in ``__init__`` (heavy
            init defers to :meth:`on_session_start` per ADR-001
            §Consequences), so firing the guard here would reject
            perfectly working Python installations that simply cannot
            load extensions. Issue 02-03's ``on_session_start`` body
            will call the guard before its first ``open_lcm_db()``
            invocation — matching ADR-004 §Consequences' "before any DB
            open attempt" requirement literally.
        """
        # Constructor args
        self.hermes_home: Path = (
            Path(hermes_home) if hermes_home is not None else Path.home() / ".hermes"
        )
        self.config: LcmConfig = config if config is not None else LcmConfig()

        # ------------------------------------------------------------------
        # State fields — owned by shell, used by mixins (ADR-027 §Consequences)
        # ------------------------------------------------------------------
        # DB connection — opened in on_session_start (02-03). Typed as
        # ``Optional[sqlite3.Connection]`` because pre-on_session_start
        # callers see ``None``.
        self._db: Optional[sqlite3.Connection] = None

        # The four Epic-01 stores — instantiated in on_session_start
        # (02-03) once ``self._db`` is open. Typed as Optional[...] so
        # mixin methods can guard with ``if self._conversation_store is
        # None: raise RuntimeError("on_session_start not yet called")``.
        self._conversation_store: Optional[ConversationStore] = None
        self._summary_store: Optional[SummaryStore] = None
        self._telemetry_store: Optional[CompactionTelemetryStore] = None
        self._maintenance_store: Optional[CompactionMaintenanceStore] = None

        # Per-session asyncio locks — see ADR-018 §"Per-session queue".
        # Issue 02-08 replaces the issue 02-01 ``defaultdict(asyncio.Lock)``
        # placeholder with a :class:`SessionLockRegistry` that adds a
        # refcount + lazy prune pass so the lock dict cannot grow
        # without bound on a long-running gateway (ADR-018 §"Open
        # questions" line 96–97). Critical sections in Epic 03 (ingest)
        # / Epic 04 (compact) acquire via
        # ``async with self._session_locks.acquire(session_id): ...``.
        self._session_locks: SessionLockRegistry = SessionLockRegistry()

        # Circuit-breaker state-machine (02-09). Keyed by session /
        # provider scope (Epic 04 chooses the key policy; the scaffold
        # is opaque). Values are :class:`CircuitBreaker` dataclasses.
        # Use :meth:`_get_or_create_circuit_breaker` to fetch with the
        # right config defaults applied.
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}

        # ``last_seen_message_idx`` for diff-based ingest (Epic 03).
        # Keyed by session_id; value is the index of the last message
        # the engine has already ingested. ``_on_post_llm_call`` diffs
        # ``conversation_history[idx:]`` against this on each turn.
        self._last_seen_message_idx: Dict[str, int] = {}

        # ``_compression_history`` — bounded deque of (before_tokens,
        # after_tokens) tuples, one entry per ``compress()`` call.
        # Consumed by ``should_compress`` (in :class:`_CompactMixin`) for
        # the anti-thrashing gate: when the most recent
        # ``INEFFECTIVE_RUN_LENGTH`` entries are all ineffective (saved
        # <10% of pre-compression tokens), ``should_compress`` returns
        # False even at over-threshold prompt_tokens to break the
        # hot-loop. ``maxlen`` is generously sized — we only ever
        # inspect the tail, so a small ring keeps memory bounded and
        # keeps a debugging breadcrumb. Hermes parity:
        # ``context_compressor.py`` tracks ``_ineffective_compression_count``
        # as a single counter; we keep a deque so Epic 04's real
        # algorithm has a debugging-friendly window to consult.
        self._compression_history: Deque[Tuple[int, int]] = deque(maxlen=16)

        # ------------------------------------------------------------------
        # Token-state attributes inherited from the ABC (run_agent.py reads
        # these directly). Class-level defaults are 0; we re-declare as
        # instance attrs so the no-op ``update_from_response`` can write
        # them without first falling back to the class default.
        # ABC §"Token state" line 46-51.
        # ------------------------------------------------------------------
        self.last_prompt_tokens: int = 0
        self.last_completion_tokens: int = 0
        self.last_total_tokens: int = 0
        self.threshold_tokens: int = 0
        self.context_length: int = 0
        self.compression_count: int = 0

        # ------------------------------------------------------------------
        # Cache-aware token state (ADR-015 patch #4)
        # ------------------------------------------------------------------
        # Hermes today doesn't always forward cache_read/write to
        # ``update_from_response``. When the patch lands upstream (or a
        # provider returns Anthropic-native usage with cache fields), these
        # capture the values for the cache-aware deferral gate (Epic 04).
        # ``cache_aware`` flips to ``True`` once we observe at least one
        # cache field; downstream policy can then trust the read/write
        # counters. Until then the gate degrades to the conservative
        # always-compact-when-over-threshold policy.
        self.last_cache_read_tokens: int = 0
        self.last_cache_write_tokens: int = 0
        self.cache_aware: bool = False

        # ------------------------------------------------------------------
        # 02-02 state fields — identity, migration/feature flags, compiled
        # session-filter patterns, assembly-state dicts, log-dedup set.
        # See ``epics/02-engine-skeleton/02-02-engine-state.md`` for the
        # canonical table. The JSONL-derived TS fields are deliberately
        # absent per the same spec §Dropped fields.
        # ------------------------------------------------------------------

        # Identity + capability record. Default ``owns_compaction=True``;
        # Epic 04's compaction-failure handler may eventually flip this
        # to False if migrations or stores cannot be brought up (the
        # ContextEngineInfo dataclass is frozen, so use
        # ``dataclasses.replace`` in that path).
        self.info: ContextEngineInfo = ContextEngineInfo()

        # ``migrated`` — True once ``run_lcm_migrations`` succeeds.
        # Updated by :meth:`_LifecycleMixin.on_session_start` (02-03)
        # after the migration ladder returns; stays False here at
        # construction time since heavy init is deferred (ADR-001).
        self.migrated: bool = False

        # ``fts5_available`` — sqlite FTS5 extension presence.
        # Updated by :meth:`_LifecycleMixin.on_session_start` (02-03)
        # from ``get_lcm_db_features(conn).fts5_available`` after the
        # connection opens. Default ``True`` at construction is the
        # optimistic case; the lifecycle body will overwrite it based
        # on the real probe before any store reads run. Epic 03/04
        # readers consult this when deciding between FTS5 and the
        # LIKE-fallback paths in the stores.
        self.fts5_available: bool = True

        # ``ignore_session_patterns`` — compiled regex list from
        # ``config.ignore_session_patterns`` (a ``list[str]``).
        # Matched against ``session_id`` to skip the LCM pipeline
        # entirely for sessions that should never be tracked (CI runs,
        # benchmark scripts, etc.). Used by Epic 03's ingest gate.
        self.ignore_session_patterns: List[re.Pattern[str]] = [
            re.compile(p) for p in self.config.ignore_session_patterns
        ]

        # ``stateless_session_patterns`` — compiled regex list from
        # ``config.stateless_session_patterns`` (a ``list[str]``).
        # Matched against ``session_id`` to bypass DB writes (but still
        # observe). Used by Epic 03's ingest gate. Separate from
        # ``ignore_session_patterns`` so an operator can keep the
        # observability layer (token telemetry, log breadcrumbs) while
        # skipping the persistence layer.
        self.stateless_session_patterns: List[re.Pattern[str]] = [
            re.compile(p) for p in self.config.stateless_session_patterns
        ]

        # ``_previous_assembled_messages_by_conversation`` — last
        # assembled message list per conversation id. Used by
        # :class:`_AssembleMixin` in Epic 03 for prefix-stability
        # diagnostics (catching cases where the deterministic-assembly
        # invariant breaks — the same turn assembling to a different
        # prefix on consecutive calls, which would invalidate the cache
        # contract). Empty dict at construction; populated per
        # conversation as the assembly hook runs.
        self._previous_assembled_messages_by_conversation: Dict[int, Any] = {}

        # ``_stable_orphan_stripping_ordinals_by_conversation`` —
        # per-conversation boundary for orphan-tool-result stripping
        # in Epic 03's assembly. Used by :class:`_AssembleMixin` to
        # guarantee that once an ordinal is declared "stable boundary
        # for orphan stripping", subsequent assemblies don't move it
        # backward (which would re-strip already-stable tool-result
        # rows). Empty dict at construction; populated per conversation.
        self._stable_orphan_stripping_ordinals_by_conversation: Dict[int, int] = {}

        # ``_cache_context_unknown_logged`` — per-process dedupe set
        # for the "cache context unknown" info-level log warning.
        # Without dedupe, every turn on a cache-unaware provider would
        # emit the same warning, drowning normal logs. Keyed by
        # conversation id; an entry's presence means the warning
        # already fired for that conversation this process. Cleared
        # only on process exit (no per-session reset — the warning
        # is genuinely once-per-process useful).
        self._cache_context_unknown_logged: Set[int] = set()

        # ------------------------------------------------------------------
        # 03-09 — ADR-010 always-on substitution mode detection
        # ------------------------------------------------------------------
        # Per ADR-010, lossless-hermes has TWO paths for per-turn assembly
        # substitution:
        #
        #   * **Production (Option B)** — Hermes ``ContextEngine`` ABC
        #     exposes a ``preassemble(messages, budget_tokens)`` method
        #     (upstream PR #24949). When present, ``run_agent.py`` calls
        #     it BEFORE ``pre_llm_call`` on every turn. We override it on
        #     :class:`_AssembleMixin` and route to ``self._assemble(...)``.
        #
        #   * **Experimental (Option A)** — when ``preassemble`` is
        #     ABSENT and the operator sets
        #     ``experimental.always_on_via_compress: true``, we force
        #     ``should_compress()`` to return ``True`` every turn so
        #     ``run_agent.py:10264`` calls ``compress()`` — whose return
        #     value REPLACES the live message list (the substitution
        #     mechanism). This path has documented side effects
        #     (session-ID rotation per turn, memory provider re-extraction,
        #     compression-count warnings, log spam — see ADR-010 §"Option
        #     A"). Not production-grade.
        #
        # We detect Hermes's ABC capability ONCE at construction time and
        # cache the result on ``self``. Subsequent ``should_compress`` /
        # ``compress`` calls branch on these flags without re-importing
        # or re-attribute-checking.
        #
        # Hermes 24949 patch adds ``preassemble`` to ``ContextEngine`` as
        # a default no-op method. We detect by ``hasattr`` on the ABC
        # itself — if the merged Hermes installation carries the method,
        # ``ContextEngine.preassemble`` resolves. The Hermes-less bridge
        # exposes ``ContextEngine`` as a stub :class:`type` with no
        # ``preassemble`` attribute, so this evaluates to False (the
        # Hermes-less story degrades cleanly: assemble is reachable via
        # tests / engine fixtures but not via a real Hermes hook
        # invocation).
        self._has_preassemble: bool = hasattr(ContextEngine, "preassemble")

        # The experimental flag is read from config at construction time
        # so per-turn ``should_compress`` / ``compress`` paths don't
        # re-traverse the config object every call. Operators who flip
        # this at runtime via ``cfg_set`` must restart the engine to take
        # effect — by design (ADR-010 §Open questions item 1, "mode
        # switch at runtime"). The flag is gated by ABSENCE of
        # ``preassemble``: if Hermes has ``preassemble``, the experimental
        # path is unreachable regardless of the config setting (the
        # production path is strictly better; we silently prefer it).
        self._experimental_always_on_via_compress: bool = bool(
            getattr(self.config, "experimental_always_on_via_compress", False)
        )

        # Per-process dedupe state for the experimental-mode rate-limited
        # warning. Per ADR-010 §"Path 2" the warning fires once-per-engine
        # at startup AND once per minute thereafter (so operators don't
        # forget the path is broken, but don't drown in log spam either).
        # We carry a monotonic-clock timestamp of the last warning emission
        # and the 60s cooldown is a class constant in ``assemble.py``.
        self._last_experimental_warn_ts: float = 0.0

        # ------------------------------------------------------------------
        # 03-09 startup-time log: state which always-on substitution mode
        # is active. The log is informational (production mode), warning
        # (experimental mode), or warning (mode disabled — overflow-only).
        # Operators scanning startup logs can confirm which path the
        # engine took before any per-turn behavior surfaces.
        # ------------------------------------------------------------------
        if self._has_preassemble:
            logger.info(
                "[lcm] always-on substitution: production mode via "
                "ContextEngine.preassemble (ADR-010 Option B)."
            )
        elif self._experimental_always_on_via_compress:
            logger.warning(
                "[lcm] always-on substitution: EXPERIMENTAL mode via "
                "force-compress (ADR-010 Option A). Session ID rotates "
                "every turn; memory provider lineage breaks; "
                "compression-count warnings will fire. NOT FOR PRODUCTION. "
                "Disable by removing `experimental_always_on_via_compress` "
                "from $HERMES_HOME/config.yaml under `lossless_hermes:`."
            )
        else:
            logger.warning(
                "[lcm] always-on substitution DISABLED: upstream Hermes "
                "lacks `preassemble` ABC method (ADR-010 patch pending — "
                "see PR #24949), and `experimental_always_on_via_compress` "
                "is False. Plugin runs as OVERFLOW-COMPACTOR ONLY — no "
                "per-turn DAG substitution. Set `experimental_always_on_"
                "via_compress: true` in $HERMES_HOME/config.yaml under "
                "`lossless_hermes:` to validate substitution behavior "
                "(at the cost of session-ID rotation per turn)."
            )

    # ABC §Core interface -----------------------------------------------------
    # ``compress`` and ``should_compress`` bodies live in :class:`_CompactMixin`
    # (compact.py). At 02-01 those are no-op passthroughs matching 00-06;
    # Epic 04 fills in the real compaction algorithm.

    # Circuit-breaker helper (02-09) ----------------------------------------

    def _get_or_create_circuit_breaker(self, key: str) -> CircuitBreaker:
        """Fetch the breaker for ``key``, creating it with config defaults.

        This is the only blessed entry point for Epic 04 callers (the
        summarize.py compaction path will catch ``LcmProviderAuthError``
        and call ``breaker.record_failure()`` on the result). Direct
        ``self._circuit_breakers[key]`` access is permitted but loses
        the config-driven threshold/cooldown setup.

        The breaker is configured from ``self.config``:

        * ``threshold = self.config.circuit_breaker_threshold`` (default 5)
        * ``cooldown_s = self.config.circuit_breaker_cooldown_ms / 1000``
          (default 1800.0s = 30 min)

        Subsequent calls with the same ``key`` return the same instance
        (no re-config). Different keys get independent breakers.

        Maps to engine.ts:getCircuitBreakerState (line 1963).

        Args:
            key: Opaque breaker key. Epic 04's policy choice (likely
                ``f"{provider}/{model}"`` or
                ``f"{session_id}:{provider}/{model}"``).

        Returns:
            The :class:`CircuitBreaker` for ``key``.
        """
        # ``dict.setdefault`` is atomic under the CPython GIL — the
        # lookup-and-insert runs as a single bytecode op, so two
        # concurrent callers cannot both insert. The earlier
        # ``get``-then-``[key] =`` pair was a TOCTOU race: both callers
        # could see ``None`` from ``get`` and the loser's freshly
        # constructed instance would silently overwrite the winner's
        # (or vice versa), with already-issued references diverging.
        # ``setdefault`` collapses both steps into one atomic op.
        #
        # The eagerly-constructed-then-discarded loser instance is
        # acceptable: ``CircuitBreaker`` has no side effects in
        # ``__init__`` (just a small alloc + ``threading.Lock()``), and
        # the loser is dropped on return.
        breaker = self._circuit_breakers.setdefault(
            key,
            CircuitBreaker(
                threshold=self.config.circuit_breaker_threshold,
                cooldown_s=self.config.circuit_breaker_cooldown_ms / 1000.0,
            ),
        )
        return breaker

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """Record token usage from an API response.

        02-04 extension over 00-06/02-01: in addition to
        ``prompt_tokens``/``completion_tokens``/``total_tokens``, also
        captures cache-aware fields (``cache_read_tokens`` and
        ``cache_write_tokens``) when the provider — or Hermes after
        ADR-015 patch #4 lands — forwards them. The cache fields feed
        the cache-aware compaction deferral gate (Epic 04). When the
        fields are absent, ``cache_aware`` stays ``False`` and the gate
        degrades to a conservative always-compact-when-over-threshold
        policy. No crash on missing fields — graceful degradation.

        Tolerates THREE usage shapes simultaneously (the engine has no
        signal which provider Hermes is wrapping):

        1. **OpenAI Chat** — ``prompt_tokens``, ``completion_tokens``,
           ``total_tokens``. No cache fields native to this shape.
        2. **Anthropic native** — ``input_tokens``, ``output_tokens``,
           ``cache_creation_input_tokens``, ``cache_read_input_tokens``.
           The cache fields are what ADR-015 patch #4 normalizes to the
           shared ``cache_read_tokens`` / ``cache_write_tokens`` keys.
        3. **OpenAI Responses (Codex)** — ``prompt_tokens``,
           ``completion_tokens``, ``prompt_tokens_details.cached_tokens``.
           The Codex harness reports cache hits via a nested dict.

        Additionally accepts the **Hermes-normalized** keys
        ``cache_read_tokens`` / ``cache_write_tokens`` (post-ADR-015
        patch #4) — these take precedence over the provider-native
        fields when present, matching the documented forwarding shape.

        If ``total_tokens`` is missing it is computed from prompt +
        completion. Maps to ``engine.ts`` per-turn token recording
        inside ``afterTurn`` (lines 6473–6638) — specifically the
        ``updateCompactionTelemetry`` path that feeds cache state to
        the deferral gate.

        Args:
            usage: The ``usage`` dict from the LLM response.
        """
        # --- Prompt / completion / total -----------------------------------
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        total = usage.get("total_tokens", prompt + completion) or (prompt + completion)
        self.last_prompt_tokens = prompt
        self.last_completion_tokens = completion
        self.last_total_tokens = total

        # --- Cache-aware fields (ADR-015 patch #4) -------------------------
        # Precedence: Hermes-normalized keys (when patch #4 forwards them)
        # win over provider-native shapes. Anthropic-native is checked
        # before the OpenAI Responses (Codex) nested form. The presence
        # of ANY cache field flips ``cache_aware`` to True so the
        # deferral gate knows it has trustworthy signal.
        cache_read: Optional[int] = None
        cache_write: Optional[int] = None

        # 1. Hermes-normalized (post-patch #4)
        if "cache_read_tokens" in usage:
            cache_read = usage.get("cache_read_tokens") or 0
        if "cache_write_tokens" in usage:
            cache_write = usage.get("cache_write_tokens") or 0

        # 2. Anthropic-native: cache_read_input_tokens / cache_creation_input_tokens
        if cache_read is None and "cache_read_input_tokens" in usage:
            cache_read = usage.get("cache_read_input_tokens") or 0
        if cache_write is None and "cache_creation_input_tokens" in usage:
            cache_write = usage.get("cache_creation_input_tokens") or 0

        # 3. OpenAI Responses (Codex): prompt_tokens_details.cached_tokens
        # Only the read counter is available in this shape — Codex
        # doesn't expose a cache-write counter, so cache_write stays
        # absent and the gate treats it as 0 (conservative).
        if cache_read is None:
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict) and "cached_tokens" in details:
                cache_read = details.get("cached_tokens") or 0

        if cache_read is not None or cache_write is not None:
            self.cache_aware = True
            self.last_cache_read_tokens = cache_read if cache_read is not None else 0
            self.last_cache_write_tokens = cache_write if cache_write is not None else 0
        else:
            # No cache signal this turn. Per the 02-04 spec acceptance
            # criterion ("Missing cache fields → cache_aware = False"),
            # the flag reflects the CURRENT call, not a session sticky
            # bit. The cache-aware deferral gate (Epic 04) reads this
            # per-turn; if the most recent turn lacked cache data the
            # gate falls back to the conservative path even if earlier
            # turns reported cache hits. We also zero the per-turn
            # counters so stale values can't feed the gate.
            self.cache_aware = False
            self.last_cache_read_tokens = 0
            self.last_cache_write_tokens = 0

    # ABC §Tools (defaults are fine; explicit overrides for 02-01 clarity) ---

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return an empty tool list for 02-01.

        The 8 ``lcm_*`` tools (lcm_grep, lcm_describe, lcm_expand,
        lcm_synthesize_around, lcm_get_entity, lcm_search_entities,
        lcm_compact, lcm_conversation_scope) all land in Epic 06.
        Returning ``[]`` matches the ABC default but is explicit here so
        the contract is obvious.

        Returns:
            Empty list at 02-01.
        """
        return []

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Dispatch an LCM tool — with a belt-and-suspenders ingest prelude.

        **Issue 03-03 (ADR-009 §Decision "Option C"):** before any
        per-tool dispatch this method reads ``kwargs.get("messages")``
        and runs the same diff-against-cursor / ``_ingest_batch`` body
        that ``post_llm_call`` runs (via
        :meth:`_IngestMixin._ingest_from_handle_tool_call`). The
        prelude covers tool-only turns where the ``post_llm_call`` hook
        does NOT fire (gated on ``final_response and not interrupted``
        at ``run_agent.py:15407`` — Ctrl-C, max-iterations,
        no-final-response). Idempotent under the cursor — double-firing
        both hooks is harmless because the second caller through the
        per-session sync lock re-reads the cursor and sees no new
        messages.

        Session-id resolution: Hermes today passes only
        ``messages=messages`` at ``run_agent.py:11249`` (no
        ``session_id`` / ``sender_id`` in the kwargs). The
        ``kwargs.get("session_id") or kwargs.get("sender_id")`` chain
        is **forward-compat**: if either key is later added to the
        Hermes hook call, this code picks it up automatically. When
        neither is present (the v0.1 reality) the prelude is a no-op
        and only the existing per-tool dispatch runs — matching the
        spec AC "Missing ``session_id`` AND ``sender_id``: no-op
        ingest".

        Tool dispatch itself: at 02-01 / 03-03 there are no LCM tools
        registered (:meth:`get_tool_schemas` returns ``[]``), so reaching
        the dispatch step implies a programmer error or test
        invocation. The body raises :class:`NotImplementedError` for
        any tool ``name``; real tool handling lands in Epic 06 (the
        8 LCM tools: ``lcm_grep`` / ``lcm_describe`` / ``lcm_expand`` /
        ``lcm_compact`` / ``lcm_synthesize_around`` /
        ``lcm_get_entity`` / ``lcm_search_entities`` /
        ``lcm_conversation_scope``).

        **Sync, not async.** Hermes's ``run_agent.py:11249`` calls this
        method synchronously inside the tool-dispatch loop. The
        :meth:`_ingest_from_handle_tool_call` body acquires the
        per-session **sync** lock via
        :meth:`SessionLockRegistry.acquire_sync` (added at issue 03-02
        per the PR #34 sync-conversion). No ``asyncio.run`` is needed
        — the spec's example was OUTDATED at the time it was written
        (it predated PR #34 + #42). See ``epics/03-ingest-assembly
        /03-03-ingest-from-handle-tool-call.md`` §"Sync override".

        Args:
            name: Tool name being dispatched. At 03-03 this is one of
                the future LCM tool names (Epic 06 binds them); the
                router only routes through this method for LCM-owned
                tool names (per ``docs/reference/hermes-hooks.md`` line
                182).
            args: Tool arguments. Ignored by the 03-03 ingest prelude.
            **kwargs: May include ``messages`` (Hermes today, per
                ``run_agent.py:11249``) and forward-compat
                ``session_id`` / ``sender_id``. All read defensively;
                missing keys default to no-op.

        Raises:
            NotImplementedError: Always (at 03-03; Epic 06 fills in
                the real dispatch). The ingest prelude is best-effort
                and never raises — it logs + returns silently per
                :meth:`_ingest_from_handle_tool_call`'s observer-only
                contract.
        """
        # --- Issue 03-03 ingest prelude (ADR-009 Option C) ---------------
        # Pulled defensively from kwargs so the prelude can NEVER raise
        # on a malformed dispatch shape (a future Hermes change that
        # drops the ``messages`` kwarg, an explicit test invocation that
        # passes only positional args, etc.). All three checks must
        # pass for the prelude to fire:
        #   1. ``messages`` is present + truthy
        #   2. either ``session_id`` or ``sender_id`` is present
        #   3. (deferred to the prelude itself) the session_id is non-empty
        # The prelude runs BEFORE any tool dispatch so the assembler
        # sees the user's pre-tool turn even if the loop exits via
        # Ctrl-C / max-iterations / no-final-response before the
        # ``post_llm_call`` hook would fire.
        messages = kwargs.get("messages")
        session_id = kwargs.get("session_id") or kwargs.get("sender_id")
        if messages and session_id:
            # Sync: PR #34 (merged 2026-05-13) + PR #42 (merged
            # 2026-05-14) converted ingest off async. The prelude runs
            # on the calling thread (Hermes's tool-dispatch loop) and
            # acquires the per-session sync lock for the diff window.
            self._ingest_from_handle_tool_call(session_id, messages)

        # --- Existing per-tool dispatch ---------------------------------
        # No LCM tools at 02-01 / 03-03 — Epic 06 fills in the real
        # dispatch ladder. The override raises for ANY name so a stray
        # dispatch surfaces loudly (rather than silently returning a
        # JSON error string per the ABC default). The error message
        # names both the attempted tool and the epic that lands the
        # real handler.
        raise NotImplementedError(f"handle_tool_call({name!r}, ...): tools land in Epic 06")
