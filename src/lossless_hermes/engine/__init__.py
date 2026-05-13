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
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.hermes_bridge import ContextEngine
from lossless_hermes.store.compaction_maintenance import CompactionMaintenanceStore
from lossless_hermes.store.compaction_telemetry import CompactionTelemetryStore
from lossless_hermes.store.conversation import ConversationStore
from lossless_hermes.store.summary import SummaryStore

from .assemble import _AssembleMixin
from .compact import _CompactMixin
from .ingest import _IngestMixin
from .lifecycle import _LifecycleMixin
from .session_locks import SessionLockRegistry

__all__ = ["APPLE_SYSTEM_PYTHON_MSG", "LCMEngine"]

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

        # Circuit-breaker scaffold — body lands in 02-09. Keyed by
        # session/provider scope; values are CircuitBreakerState records
        # whose Python type lands with the body.
        self._circuit_breakers: Dict[str, Any] = {}

        # ``last_seen_message_idx`` for diff-based ingest (Epic 03).
        # Keyed by session_id; value is the index of the last message
        # the engine has already ingested. ``_on_post_llm_call`` diffs
        # ``conversation_history[idx:]`` against this on each turn.
        self._last_seen_message_idx: Dict[str, int] = {}

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

    # ABC §Core interface -----------------------------------------------------
    # ``compress`` and ``should_compress`` bodies live in :class:`_CompactMixin`
    # (compact.py). At 02-01 those are no-op passthroughs matching 00-06;
    # Epic 04 fills in the real compaction algorithm.

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """Record token usage from an API response.

        02-01 implementation (unchanged from 00-06): stores
        ``prompt_tokens``, ``completion_tokens``, and ``total_tokens``
        in the standard instance attributes ``run_agent.py`` reads
        directly. No telemetry-store write — that lands in Epic 04
        (compaction telemetry).

        Tolerates both ``prompt_tokens``/``completion_tokens`` (OpenAI
        style) and ``input_tokens``/``output_tokens`` (Anthropic style)
        keys; if ``total_tokens`` is missing it is computed from the
        parts. Maps to engine.ts behavior for the ``last_*_tokens``
        fields under §"State owned by LcmContextEngine"
        (porting-guides/engine.md lines 21-50).

        Args:
            usage: The ``usage`` dict from the LLM response.
        """
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        total = usage.get("total_tokens", prompt + completion) or (prompt + completion)
        self.last_prompt_tokens = prompt
        self.last_completion_tokens = completion
        self.last_total_tokens = total

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
        """Raise :class:`NotImplementedError` — no tools at 02-01.

        The ABC default returns a JSON error string; we override to
        raise instead so a stray dispatch (which should not happen since
        :meth:`get_tool_schemas` returns ``[]``) surfaces loudly. Real
        tool handling lands in Epic 06.

        Args:
            name: Tool name being dispatched.
            args: Tool arguments.
            **kwargs: May include ``messages`` per ABC contract.

        Raises:
            NotImplementedError: Always at 02-01.
        """
        raise NotImplementedError(f"handle_tool_call({name!r}, ...): tools land in Epic 06")
