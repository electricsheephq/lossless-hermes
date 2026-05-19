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

import json
import logging
import re
import sqlite3
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Final, List, Optional, Set, Tuple

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.hermes_bridge import ContextEngine
from lossless_hermes.store.compaction_maintenance import CompactionMaintenanceStore
from lossless_hermes.store.compaction_telemetry import CompactionTelemetryStore
from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.store.summary import SummaryStore
from lossless_hermes.tools import get_tool_schemas as _registry_get_tool_schemas

from lossless_hermes.plugin.needs_compact_gate import (
    TOKEN_GATE_TOOLS,
    run_with_token_gate,
)
from lossless_hermes.plugin.token_state import (
    get_runtime_context as _token_state_get_runtime_context,
)
from lossless_hermes.plugin.token_state import (
    record_llm_output as _token_state_record_llm_output,
)

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
    "RuntimeContext",
    "TOKEN_GATE_TOOLS",
    "TOOL_DISPATCH",
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
    version: str = "0.1.1"
    owns_compaction: bool = True


# ---------------------------------------------------------------------------
# RuntimeContext — token-budget snapshot for the per-call gate (issue 06-02)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Per-call snapshot of current token usage + budget for the gate.

    Issue 06-02 introduces this as the value :meth:`LCMEngine.get_runtime_context`
    returns. The token-gate middleware (porting guide ``tools.md`` lines
    597–610, ``needs-compact-gate.ts`` in TS) consumes
    ``current_token_count`` and ``token_budget`` to decide whether to
    refuse a tool call that would push the projected context-ratio over
    the ``REFUSAL_THRESHOLD`` (0.92).

    Per [ADR-TOOLS-05](../porting-guides/tools.md), the source of these
    numbers is the in-memory token-state cache that
    :meth:`LCMEngine.update_from_response` populates per-turn. At v0.1.0
    the cache is keyed by ``session_id`` and surfaces the most recent
    LLM-response prompt-token count plus the configured context budget
    (Hermes's ``model_context_length``). When neither is available (e.g.
    pre-first-response, or under a stateless test fixture), both fields
    are ``None`` — the gate degrades to "skip the gate" (see the
    porting guide's "Skipped (bypassed) when ``currentTokenCount`` or
    ``tokenBudget`` is undefined" note).

    Attributes:
        current_token_count: Most-recent ``last_prompt_tokens`` for the
            session. ``None`` means no LLM response observed yet.
        token_budget: Configured context budget (model context length
            minus a safety reserve). ``None`` means the engine has not
            been told the budget yet (test-fixture path).
    """

    current_token_count: Optional[int] = None
    token_budget: Optional[int] = None


# ---------------------------------------------------------------------------
# TOOL_DISPATCH — module-level handler registry (issue 06-02)
# ---------------------------------------------------------------------------
#
# Per the issue spec (``epics/06-tools/06-02-tool-dispatch-table.md``)
# and the porting guide (``docs/porting-guides/tools.md`` lines 622–688),
# this is the canonical handler registry consulted by
# :meth:`LCMEngine.handle_tool_call`. Per-tool issues 06-07 through 06-14
# register their handler here at import time::
#
#     # In tools/grep.py at module scope:
#     from lossless_hermes.engine import TOOL_DISPATCH
#     TOOL_DISPATCH["lcm_grep"] = handle_lcm_grep
#
# At 06-02 the registry is EMPTY. The :func:`get_tool_schemas` registry
# (``lossless_hermes.tools.TOOL_SCHEMAS``) is also empty — per-tool
# ports populate both atomically as they land.
#
# **Why module-level, not class-level**: tests and per-tool ports both
# need write access. A class attribute would mean every test that
# stubs a handler has to know the subclass shape; a module-level dict
# keeps the registration site one stable line per tool. Per ADR-TOOLS-03
# in tools.md, the middleware lives in :meth:`handle_tool_call` (Option
# B), and the dispatch table is the obvious public seam for per-tool
# wiring.
#
# **The signature**: per the porting guide line 654–687, each handler
# is called as ``handler(args, **kwargs)`` where kwargs includes
# ``db``, ``retrieval``, ``voyage``, ``deps``, ``session_key``,
# ``runtime_ctx``, and ``messages``. The exact kwargs list is finalized
# when per-tool issues land — at 06-02 the dispatch passes through
# whatever ``LCMEngine.handle_tool_call`` was called with (plus the
# resolved ``session_key`` + ``runtime_ctx``). Handlers MUST return a
# JSON string (per :py:func:`lossless_hermes.tools._common.tool_result`).
#
# **v0.1.0 omission**: per [ADR-012](../../docs/adr/012-subagent-defer.md),
# ``lcm_expand_query`` is NOT registered in v0.1.0 — only 7 of the 8
# LCM tools ship. The dispatch entry is left absent (the lookup falls
# through to the unknown-tool error path).

TOOL_DISPATCH: Final[Dict[str, Callable[..., str]]] = {}


# ---------------------------------------------------------------------------
# TOKEN_GATE_TOOLS is re-exported (canonical definition lives in
# ``lossless_hermes.plugin.needs_compact_gate`` — see import block above
# and ``__all__``). Per 06-03 (PR #95), the gate's authoritative
# membership set is owned by the middleware module: the engine simply
# consults it at invocation time. Re-exporting it from this module
# keeps the 06-02 spec's public-surface guarantee intact (tests import
# ``TOKEN_GATE_TOOLS`` from ``lossless_hermes.engine``).
# ---------------------------------------------------------------------------


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

        # ``current_session_id`` — most-recent Hermes session_id seen by
        # ``on_session_start``. Used by Epic 08 ``/lcm`` handlers to resolve
        # "the current conversation" without a ``PluginCommandContext`` to
        # consult (Hermes's slash-command hook signature is ``(raw_args)
        # -> str | None`` — no session context piggybacks).
        # Per ``docs/porting-guides/plugin-glue.md`` §"Per-subcommand
        # translation table" line 650, the TS ``ctx.sessionId`` /
        # ``ctx.sessionKey`` maps to this engine-tracked field. Stays
        # ``None`` before the first ``on_session_start`` (CLI pre-first-
        # message; gateway with no active conversation) — handlers must
        # treat ``None`` as "no active conversation" rather than
        # rendering an "id=None" field. Cleared on ``on_session_end``
        # for symmetry with the DB-close path.
        self.current_session_id: Optional[str] = None

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

        # --- Issue 06-03 token-state cache anchor -----------------------
        # Feed the per-session token-state cache so the
        # ``run_with_token_gate`` middleware can read fresh
        # ``current_token_count`` + ``token_budget`` on the next tool
        # dispatch. ``record_llm_output`` accepts the same ``usage``
        # dict; it pulls ``input + cacheRead + cacheWrite`` (the LCM
        # composition — output tokens are the LLM's response, not
        # context) and stamps it under the current session-key. Empty
        # session-key is a no-op — by design, the cache is keyed by
        # cross-conversation identity that Hermes plumbs through
        # ``kwargs["session_key"]`` when present.
        if self.current_session_id:
            # token_budget on the cache snapshot is the engine's
            # ``context_length`` (set by the engine's existing flow when
            # a model identifier resolves); pass ``None`` when 0 to
            # signal "unknown budget" -> gate bypasses cleanly.
            budget = self.context_length if self.context_length > 0 else None
            _token_state_record_llm_output(
                session_key=self.current_session_id,
                usage=usage,
                token_budget=budget,
            )

    # ABC §Tools -------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return the registered LCM tool schemas (OpenAI function-call format).

        Per [ADR-024](../../docs/adr/024-project-layout.md) and the
        porting guide ``tools.md`` lines 642–652, this method delegates
        to the package-level :func:`lossless_hermes.tools.get_tool_schemas`
        registry. Per-tool ports (06-07..06-14) append their
        ``LCM_<TOOL>_SCHEMA`` dicts to that registry at import time so
        the engine doesn't need to know which tools have been ported.

        Order is stable — :func:`get_tool_schemas` preserves insertion
        order (CPython 3.7+ dict and list semantics), and tests pin that
        ordering. Per ADR-029, the registry pattern decouples engine
        wiring from per-tool schema content so future tool additions
        only touch the per-tool module + registration line.

        At 06-02 the registry is empty (per-tool ports populate it as
        they land). Once 06-07..06-14 have all merged, the v0.1.0
        surface is 7 schemas (no ``lcm_expand_query`` per ADR-012).

        Returns:
            A FRESH list (callers may mutate freely without side-effects
            on the registry). Each entry is an OpenAI-format dict with
            ``name`` / ``description`` / ``parameters`` keys.
        """
        return _registry_get_tool_schemas()

    @property
    def _current_session_key(self) -> Optional[str]:
        """Most-recent session identifier seen by ``on_session_start``.

        Used by :meth:`handle_tool_call` as the fallback when the tool
        kwargs don't carry ``session_key`` / ``session_id`` /
        ``sender_id``. The TS source's ``runWithTokenGate`` reads
        ``sessionKey`` from a per-engine context the OpenClaw gateway
        installs (``ctx.sessionKey`` in the factory closures, per
        ``src/plugin/index.ts:2415``); Hermes does not surface that on
        the ``handle_tool_call`` hook, so the engine falls back to the
        most-recent ``on_session_start`` argument that's already
        captured on :attr:`current_session_id` (see the field doc in
        :meth:`__init__`).

        Implemented as a read-only property so per-tool handlers (or
        tests) cannot accidentally re-bind it. The single writer is
        :meth:`_LifecycleMixin.on_session_start`.

        Returns:
            The most-recent session id, or ``None`` if no session has
            started yet (CLI pre-first-message; bare engine tests).
        """
        return self.current_session_id

    def get_runtime_context(self, session_key: Optional[str]) -> RuntimeContext:
        """Return a :class:`RuntimeContext` snapshot for ``session_key``.

        Per the porting guide ``tools.md`` ADR-TOOLS-05 (lines 757):
        TS plumbs ``currentTokenCount + tokenBudget`` from an
        ``llm_output`` hook into a per-session in-memory cache. The
        Python equivalent reads :attr:`last_prompt_tokens` (populated
        by :meth:`update_from_response` per turn) and
        :attr:`threshold_tokens` (set by Hermes / Epic 04). Both are
        sticky across turns; when either is zero (pre-first-response,
        bare engine fixture), the corresponding field is ``None`` and
        the gate skips the refusal check (matching TS line 605 "Skipped
        (bypassed) when ``currentTokenCount`` or ``tokenBudget`` is
        undefined").

        At 06-02 the cache is engine-scoped (one cache for the whole
        engine — :attr:`current_session_id` is the only "current"
        session). Per the porting guide, when the v0.1.0 gateway lifts
        the per-session cache out, this method will be the only seam
        that changes — handlers and the gate read through here.

        **Wave-N provenance** (per ADR-029): the TS source ties this
        getter to the Wave-14 ``getTokenStateRuntimeContext`` cache and
        the Wave-12 W2A1 P0 #2 audit fix that wired
        ``lcm_synthesize_around`` onto the token-accounting bus
        (``src/plugin/index.ts:2451`` in lossless-claw at ``1f07fbd``).
        The Python port consolidates both: every gated tool reaches
        the gate through this method, so the W2A1 escape ("synthesize
        was previously off the token-accounting bus") is structurally
        prevented — there's no path to dispatch a gated tool without
        going through here.

        Args:
            session_key: The session identifier the call is being made
                under. At 06-02 the value is ignored (the snapshot is
                engine-scoped); future per-session caches will use it.
                Accepted as ``None`` so the unknown-tool error path
                does not have to construct a dummy key.

        Returns:
            A :class:`RuntimeContext` with ``current_token_count`` and
            ``token_budget`` either set (when the engine has observed a
            real LLM response and Hermes has set the budget) or ``None``
            (pre-first-response / test-fixture path).
        """
        # The parameter is intentionally kept on the signature even
        # though it's unused at 06-02 — the per-tool issues 06-07..06-14
        # call into this method with the resolved session_key, and the
        # signature stability matters for that contract.
        _ = session_key

        return RuntimeContext(
            current_token_count=(self.last_prompt_tokens if self.last_prompt_tokens > 0 else None),
            token_budget=(self.threshold_tokens if self.threshold_tokens > 0 else None),
        )

    def _run_with_token_gate(
        self,
        *,
        name: str,
        handler: Optional[Callable[..., str]],
        args: Dict[str, Any],
        session_key: Optional[str],
        runtime_ctx: RuntimeContext,
        kwargs: Dict[str, Any],
    ) -> str:
        """Token-gate middleware seam — bridges to the real wrapper from 06-03.

        Per the 06-02 dispatch contract this method is the seam tests
        spy on (see ``tests/test_tool_dispatch.py``); per 06-03
        (PR #95, ``needs_compact_gate.run_with_token_gate``) the real
        gate logic — pre-call refusal evaluator, throw-tap, post-call
        accounting — lives in a module-level function so it can be
        reused outside the engine class. This method composes the two:
        the spec's per-spy contract on the method shape, plus the
        Wave-12 F5 middleware-not-decorator pattern from 06-03.

        Per ADR-029 §"Known Wave-N fixes" Wave-12 F5, the gate's
        ``current_token_count`` / ``token_budget`` source is the
        :mod:`token_state` cache (NOT the engine's ``last_prompt_tokens``
        field), so direct cache mutations are reflected on every
        invocation. The :class:`RuntimeContext` value passed to the
        handler reflects engine state per the 06-02 spec — both views
        coexist by design.

        Args:
            name: The tool name being dispatched (consumed by the
                estimator inside :func:`run_with_token_gate`).
            handler: The looked-up dispatch handler from
                :data:`TOOL_DISPATCH`, or ``None`` when the tool is not
                registered. The inner thunk routes through
                :meth:`_dispatch_tool_call` so the registry lookup
                happens at the same seam main's middleware test
                overrides — keeping both 06-02 and 06-03 tests passing.
            args: The tool arguments dict (the LLM's tool-call
                ``arguments`` after :py:func:`json.loads`).
            session_key: The resolved session identifier from
                :meth:`handle_tool_call`. May be ``None`` if neither
                kwargs nor :attr:`_current_session_key` provided one.
            runtime_ctx: The :class:`RuntimeContext` snapshot from
                :meth:`get_runtime_context`. Forwarded to the handler;
                the gate's own decision sources its numbers from
                :func:`_token_state_get_runtime_context`.
            kwargs: The remaining caller kwargs (e.g. ``messages`` for
                the ingest prelude, future plumbing). Passed through to
                the handler unmodified.

        Returns:
            Either the gate's refusal payload (when the projected ratio
            exceeds :data:`REFUSAL_THRESHOLD`) or the handler's return
            value. Both are JSON strings per the tool-result contract.
        """
        # The gate's pre-call decision sources from the token-state
        # cache — direct cache mutation (Wave-12 F5 regression test)
        # must be visible here.
        gate_view = _token_state_get_runtime_context(session_key)
        _ = handler  # handler is consumed inside _dispatch_tool_call via TOOL_DISPATCH
        return run_with_token_gate(
            tool_name=name,
            tool_params=args,
            session_key=session_key,
            current_token_count=gate_view.get("current_token_count"),
            token_budget=gate_view.get("token_budget"),
            inner=lambda: self._dispatch_tool_call(
                name,
                args,
                runtime_ctx=runtime_ctx,
                session_key=session_key,
                **kwargs,
            ),
        )

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Dispatch an LCM tool with ingest prelude + token-gate middleware.

        **Issue 06-02 dispatch contract** (porting guide ``tools.md``
        lines 622–688): the body looks up ``name`` in
        :data:`TOOL_DISPATCH`, resolves ``session_key`` from kwargs (or
        falls back to :attr:`_current_session_key`), builds the
        :class:`RuntimeContext`, and dispatches via
        :meth:`_run_with_token_gate` for tools in :data:`TOKEN_GATE_TOOLS`
        or directly to the handler otherwise. Returns
        ``json.dumps({"error": f"Unknown LCM tool: {name}"})`` for any
        unknown name. Per [ADR-017](../../docs/adr/017-sync-vs-async-db.md)
        the method is sync — inner async paths (Voyage, sub-agents)
        bridge via the engine's background event loop, not this seam.

        **Issue 03-03 (ADR-009 §Decision "Option C") prelude preserved:**
        before any per-tool dispatch this method reads
        ``kwargs.get("messages")`` and runs the same
        diff-against-cursor / ``_ingest_batch`` body that
        ``post_llm_call`` runs (via
        :meth:`_IngestMixin._ingest_from_handle_tool_call`). The
        prelude covers tool-only turns where the ``post_llm_call`` hook
        does NOT fire (gated on ``final_response and not interrupted``
        at ``run_agent.py:15407`` — Ctrl-C, max-iterations,
        no-final-response). Idempotent under the cursor — double-firing
        both hooks is harmless because the second caller through the
        per-session sync lock re-reads the cursor and sees no new
        messages.

        **Issue 06-03 token-gate middleware (Wave-12 F5):** after the
        ingest prelude and BEFORE the per-tool dispatch, the
        :func:`run_with_token_gate` wrapper consults
        :func:`get_runtime_context` and refuses the call when the
        projected post-dispatch context ratio exceeds the refusal
        threshold. The wrap is applied based on
        :data:`needs_compact_gate.TOKEN_GATE_TOOLS` membership AT
        INVOCATION TIME — not at registration time. A decorator
        approach would freeze the membership / state at plugin init,
        defeating the purpose; the wrap-at-dispatch pattern lets the
        gate see the LATEST runtime context every time.

        Session-id resolution: Hermes today passes only
        ``messages=messages`` at ``run_agent.py:11249`` (no
        ``session_id`` / ``sender_id`` in the kwargs). The
        ``kwargs.get("session_id") or kwargs.get("sender_id")`` chain
        is **forward-compat**: if either key is later added to the
        Hermes hook call, this code picks it up automatically. The
        same chain resolves the per-tool ``session_key`` argument
        (falling back to :attr:`_current_session_key` when neither
        kwarg is present). When neither is present (the v0.1 reality)
        the prelude is a no-op and only the per-tool dispatch runs —
        matching the spec AC "Missing ``session_id`` AND ``sender_id``:
        no-op ingest".

        Tool dispatch: per 06-02, :meth:`_dispatch_tool_call` looks up
        :data:`TOOL_DISPATCH` and returns
        ``json.dumps({"error": f"Unknown LCM tool: {name}"})`` for an
        unregistered name. Real handlers land per-tool in 06-07..06-14.
        The middleware wrap remains intact: the gate runs first, and if
        the dispatch raises, the wrapper taps an error-shaped payload
        into the token-state cache before re-raising (Wave-12 W2A1 P1).

        **Sync, not async.** Hermes's ``run_agent.py:11249`` calls this
        method synchronously inside the tool-dispatch loop. The
        :meth:`_ingest_from_handle_tool_call` body acquires the
        per-session **sync** lock via
        :meth:`SessionLockRegistry.acquire_sync` (added at issue 03-02
        per the PR #34 sync-conversion). No ``asyncio.run`` is needed
        at this seam — per [ADR-017](../../docs/adr/017-sync-vs-async-db.md)
        the inner async paths run on the engine's background loop.

        Args:
            name: Tool name being dispatched. The router only routes
                through this method for LCM-owned tool names (per
                ``docs/reference/hermes-hooks.md`` line 182); unknown
                names return the structured JSON error string rather
                than raising.
            args: Tool arguments (the LLM's tool-call ``arguments``
                field after :py:func:`json.loads`). Consumed by both
                the gate's per-tool estimator and the inner dispatch.
            **kwargs: May include ``messages`` (Hermes today, per
                ``run_agent.py:11249``), the forward-compat
                ``session_id`` / ``sender_id`` / ``session_key`` keys,
                and any future plumbing. All read defensively; missing
                keys default to no-op for the ingest prelude and to
                ``None`` for the ``session_key`` resolution.

        Returns:
            A JSON string. On the happy path this is the handler's
            return value (per
            :py:func:`lossless_hermes.tools._common.tool_result`). On
            an unknown tool name it is
            ``json.dumps({"error": f"Unknown LCM tool: {name}"})``.
            On a gate refusal it is the gate's refusal payload
            (when the projected ratio exceeds the threshold). Never
            raises for unknown names (per the spec AC "Returns a JSON
            string in every code path"); handler exceptions are
            payload-encoded by the per-tool middleware via
            :func:`run_with_token_gate`'s Wave-12 W2A1 P1 throw-tap.
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

        # --- 06-02 + 06-03 dispatch ------------------------------------
        # 1. Resolve the session key. Per the 06-02 spec AC, the order is:
        #    explicit kwarg → session_id kwarg → sender_id kwarg →
        #    self._current_session_key (the on_session_start fallback).
        #    All four can be None; the per-tool handler / gate is
        #    responsible for treating None as "no active session".
        session_key = (
            kwargs.get("session_key")
            or kwargs.get("session_id")
            or kwargs.get("sender_id")
            or self._current_session_key
        )

        # 2. Build the runtime-context snapshot the HANDLER will see
        #    (PR #97 contract — handlers receive a typed
        #    :class:`RuntimeContext`). The gate's decision below sources
        #    its numbers from the token-state cache via main's
        #    :func:`_token_state_get_runtime_context` so that direct
        #    cache mutations (e.g. the Wave-12 F5 middleware regression
        #    test) are reflected on every invocation.
        runtime_ctx = self.get_runtime_context(session_key)

        # 3. Strip the orchestrator-consumed kwargs before forwarding to
        #    the handler — the handler signature owns its own kwargs
        #    surface (db / retrieval / voyage / deps / messages / etc.).
        #    Per the porting guide lines 654–687, only ``messages`` is
        #    actively forwarded today; the session_id / sender_id /
        #    session_key keys are consumed at this seam.
        forwarded_kwargs = {
            k: v for k, v in kwargs.items() if k not in ("session_id", "sender_id", "session_key")
        }

        # 4. Route through the token-gate middleware OR call the handler
        #    directly. Per ADR-029 §"Known Wave-N fixes" Wave-12 F5,
        #    the wrap runs at INVOCATION time, not at registration time
        #    — :data:`TOKEN_GATE_TOOLS` membership is consulted here so
        #    the gate sees the LATEST cached state every dispatch.
        #    ``lcm_expand`` and ``lcm_compact`` bypass the gate
        #    (sub-agent dispatch + the deliberate "spend tokens to free
        #    tokens" trade respectively).
        # LCM Wave-12 F5 (2026-04-30): runWithTokenGate is middleware-not-decorator
        # so the gate state is computed at invocation time, not at
        # registration time. Original: lossless-claw/src/plugin/needs-compact-gate.ts.
        if name in TOKEN_GATE_TOOLS:
            return self._run_with_token_gate(
                name=name,
                handler=TOOL_DISPATCH.get(name),
                args=args,
                session_key=session_key,
                runtime_ctx=runtime_ctx,
                kwargs=forwarded_kwargs,
            )
        return self._dispatch_tool_call(
            name,
            args,
            runtime_ctx=runtime_ctx,
            session_key=session_key,
            **forwarded_kwargs,
        )

    def _dispatch_tool_call(self, name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Inner dispatch — routes ``name`` to its :data:`TOOL_DISPATCH` handler.

        Per the 06-02 spec, the registry lookup either:

        * returns the handler's JSON string (real dispatch), or
        * returns ``json.dumps({"error": f"Unknown LCM tool: {name}"})``
          when the name is not registered (per spec AC, NOT a Python
          exception — Hermes wraps caller-side failures in its own
          JSON envelope, so a raise here would surface as a 5xx-
          equivalent rather than as a "tool said no").

        Factored out of :meth:`handle_tool_call` so the gate middleware
        in :func:`run_with_token_gate` can wrap a single thunk. Tests
        for the middleware (see
        ``tests/plugin/test_wave12_f5_middleware_not_decorator.py``) can
        subclass :class:`LCMEngine` and override this method to return
        a known string — exercising the gate/dispatch/tap chain without
        depending on real per-tool handler bodies (which land in
        06-07..06-14).

        Args:
            name: The tool name.
            args: The tool args (forwarded to the handler verbatim).
            **kwargs: Forwarded from :meth:`handle_tool_call` after
                orchestrator-consumed keys are stripped. Includes the
                resolved ``runtime_ctx`` and ``session_key`` plus any
                handler-relevant kwargs the caller passed (e.g.
                ``messages``).

        Returns:
            The tool's JSON-encoded result string. On unknown names,
            the structured ``{"error": "Unknown LCM tool: ..."}`` JSON
            string per the 06-02 spec AC.
        """
        handler = TOOL_DISPATCH.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown LCM tool: {name}"})
        return handler(args, **kwargs)
