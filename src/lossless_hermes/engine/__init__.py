"""LCMEngine — Lossless Context Management engine class shell.

This is the v0 skeleton: a no-op ``ContextEngine`` subclass that satisfies
the Hermes ABC contract but does **no actual context management**. Every
ABC method is either:

* a **passthrough no-op** (``compress`` returns ``messages`` unchanged,
  ``should_compress`` returns ``False``, ``update_from_response`` records
  the token state but takes no other action), or
* a **stub that raises** :class:`NotImplementedError` with a message naming
  the epic that fills it in (``on_session_start``, ``on_session_end``,
  ``on_session_reset``, ``handle_tool_call``).

The class shell + state initialization + per-method docstrings establish
the call surface every later epic will fill in. Real ingest, assembly,
and compaction land in Epics 02–04.

### Why subclass via ``hermes_bridge``

We import ``ContextEngine`` from :mod:`lossless_hermes.hermes_bridge`, NOT
directly from ``agent.context_engine``. Per ADR-024 §"Hermes bridge", all
Hermes-side imports flow through ``hermes_bridge`` so future ABC churn
touches one file, not 50. In a Hermes-less env (CI, dev installs without
Hermes on the path) the bridge re-exports a stub class — see
:mod:`lossless_hermes.hermes_bridge` for the import-time fallback.

### Why a no-op for v0

ADR-001 §Consequences mandates: "Heavy init (DB open, migration ladder
run) belongs in ``ContextEngine.on_session_start``, not in ``register()``".
The v0 skeleton honors that — ``__init__`` only stores constructor args.

### Apple system Python guard

Per ADR-004 §Consequences, sqlite-vec loading requires
``sqlite3.Connection.enable_load_extension``, which Apple's system
``/usr/bin/python3`` and some pre-built CPython distributions (notably
``actions/setup-python``'s macOS builds) ship without.

The guard helper :func:`_check_sqlite_extension_loading` is deliberately
**not** wired into :meth:`LCMEngine.__init__` at v0 — there is no DB
open attempt at v0 (heavy init defers to :meth:`on_session_start` per
ADR-001 §Consequences). Firing the guard at construction time would
block legitimate Python installations from importing the package even
though no DB ever opens. Instead, the guard is exposed for the future
:meth:`on_session_start` implementation (Epic 02) to call before its
first ``open_lcm_db()`` invocation. ADR-004 §Consequences spec
("before any DB open attempt") is satisfied because v0 has no DB open
attempt at all.

The helper is independently unit-tested via
:func:`_has_sqlite_extension_loading` (the introspection hook tests can
monkey-patch — ``sqlite3.Connection`` is C-immutable).

See:

* ``docs/adr/001-plugin-distribution-model.md`` — entry-point contract.
* ``docs/adr/007-hermes-as-dependency.md`` — Hermes-less env story.
* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — sub-module split (deferred to
  Epic 02; for v0, the full shell lives in this single file).
* ``docs/porting-guides/engine.md`` — full port plan (this issue ships
  only step 1: the no-op skeleton).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.hermes_bridge import ContextEngine

__all__ = ["APPLE_SYSTEM_PYTHON_MSG", "LCMEngine"]


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
# LCMEngine — v0 no-op shell
# ---------------------------------------------------------------------------


class LCMEngine(ContextEngine):
    """Lossless Context Management engine for Hermes (v0 no-op shell).

    This is the **class skeleton** — every method is either a passthrough
    or raises :class:`NotImplementedError` with the epic that will fill
    it in. The shape is locked; the algorithm is deferred. Maps to
    ``lossless-claw/src/engine.ts`` ``LcmContextEngine`` class.

    State is stored as plain instance attributes in :meth:`__init__`. No
    DB connection is opened, no migration ladder runs, no background task
    is started — those land in Epic 02 (engine skeleton fill-in).

    Attributes:
        hermes_home: Path to ``$HERMES_HOME`` (typically ``~/.hermes/``).
            Per ADR-001, the engine takes Hermes-side path as a
            constructor arg rather than computing it itself, so tests can
            substitute a ``tmp_path``.
        config: Validated :class:`LcmConfig` instance from
            :func:`lossless_hermes.db.config.load_config`. Currently empty
            at v0; each knob lands in a subsequent PR.

    The ``name`` class attribute is the canonical engine selector — it
    matches ``context.engine: lcm`` in ``~/.hermes/config.yaml`` per
    ADR-001 §Consequences "config.yaml must also set context.engine: lcm".
    The string ``"lcm"`` is the entry-point binding the Hermes
    plugin-selection ladder (run_agent.py:2256-2287) keys off.
    """

    # ABC §Identity ----------------------------------------------------------
    # ``name`` is declared as ``@property @abstractmethod`` on the ABC, but
    # a class attribute satisfies the abstract requirement in Python (the
    # name is present on the class, which is all ``__init_subclass__``
    # checks for). Confirmed by Hermes's own ``ContextCompressor.name`` as
    # a ``@property`` and a test stub (``tests/agent/test_context_engine.py``
    # line 25) that uses a property — both work. Class attribute is the
    # idiomatic choice when the value is a constant.
    name: str = "lcm"

    # ABC §Compaction parameters (inherited, can be overridden later) -------
    # Keeping defaults from the ABC for v0. Epic 02 will override these
    # from ``self.config`` once the config knobs land.
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 8  # LCM standard, override of ABC's default 6

    def __init__(
        self,
        hermes_home: Optional[Path] = None,
        config: Optional[LcmConfig] = None,
    ) -> None:
        """Initialize the no-op engine.

        No DB connection, no migration run, no background task — heavy
        init lands in :meth:`on_session_start` per ADR-001 §Consequences.

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
            here at v0. There is no DB open attempt at v0 (heavy init
            defers to :meth:`on_session_start`), so firing the guard
            here would reject perfectly working Python installations
            that simply cannot load extensions. Epic 02's
            ``on_session_start`` body will call the guard before its
            first ``open_lcm_db()`` invocation — matching ADR-004
            §Consequences' "before any DB open attempt" requirement
            literally.
        """
        self.hermes_home: Path = (
            Path(hermes_home) if hermes_home is not None else Path.home() / ".hermes"
        )
        self.config: LcmConfig = config if config is not None else LcmConfig()

        # Token-state attributes inherited from the ABC (class-level
        # defaults are 0). Re-declared here as instance attrs so the
        # no-op ``update_from_response`` can write them without first
        # falling back to the class default. ABC §"Token state" line 46-51.
        self.last_prompt_tokens: int = 0
        self.last_completion_tokens: int = 0
        self.last_total_tokens: int = 0
        self.threshold_tokens: int = 0
        self.context_length: int = 0
        self.compression_count: int = 0

    # ABC §Core interface ----------------------------------------------------

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """Record token usage from an API response.

        v0 implementation: stores ``prompt_tokens``, ``completion_tokens``,
        and ``total_tokens`` in the standard instance attributes
        ``run_agent.py`` reads directly. No telemetry-store write — that
        lands in Epic 04 (compaction telemetry).

        Tolerates both ``prompt_tokens``/``completion_tokens`` (Anthropic-
        and OpenAI-style) keys; if ``total_tokens`` is missing it is
        computed from the parts. Maps to engine.ts behavior for the
        ``last_*_tokens`` fields under §"State owned by
        LcmContextEngine" (porting-guides/engine.md lines 21-50).

        Args:
            usage: The ``usage`` dict from the LLM response.
        """
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        total = usage.get("total_tokens", prompt + completion) or (prompt + completion)
        self.last_prompt_tokens = prompt
        self.last_completion_tokens = completion
        self.last_total_tokens = total

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Return ``False`` unconditionally for v0.

        LCM's compaction decision is driven by ``post_llm_call`` ingest +
        per-turn evaluation, not by Hermes's threshold gate (ADR-009 +
        ADR-010). The Hermes ``compress()`` path is the **overflow-
        recovery** entry point — it fires only when ``should_compress``
        returns ``True``. For v0 we always return ``False`` because there
        is no real engine to compact into; raw messages pass through
        unchanged. Real threshold logic ports in Epic 02.

        Args:
            prompt_tokens: Optional explicit token count. Ignored at v0.

        Returns:
            Always ``False`` at v0.
        """
        return False

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return ``messages`` unchanged (no-op passthrough).

        v0 is a transparent identity function — every input message list
        comes out unchanged. Maps to engine.ts ``compact()`` /
        ``executeCompactionCore`` (lines 7185-7243, 3344-3528) which port
        in Epic 04. The no-op shell preserves the ABC contract while no
        real compaction runs, so the rest of the plugin (entry point,
        config loader, hooks) can be exercised in isolation.

        Args:
            messages: The conversation message list. Returned verbatim.
            current_tokens: Ignored at v0.
            focus_topic: Ignored at v0.

        Returns:
            The input ``messages`` list, unchanged.
        """
        return messages

    # ABC §Lifecycle (override defaults with NotImplementedError) ------------

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """Lifecycle stub — raises :class:`NotImplementedError`.

        The real implementation lands in Epic 02 (engine skeleton). At
        that point it will open the SQLite DB connection, run the
        migration ladder, instantiate the stores, and apply the
        ignored/stateless session-pattern check. Maps to engine.ts
        ``bootstrap()`` (lines 4983-5424) — most JSONL fast-paths drop
        per ``docs/porting-guides/engine.md`` §"bootstrap".

        Raises:
            NotImplementedError: Always at v0.
        """
        raise NotImplementedError("on_session_start lands in Epic 02 (engine skeleton)")

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Lifecycle stub — raises :class:`NotImplementedError`.

        The real implementation lands in Epic 02. Will flush state, close
        DB connections, and persist any pending compaction work. Maps to
        engine.ts ``handleSessionEnd`` (line 7468).

        Raises:
            NotImplementedError: Always at v0.
        """
        raise NotImplementedError("on_session_end lands in Epic 02 (engine skeleton)")

    def on_session_reset(self) -> None:
        """Lifecycle stub — raises :class:`NotImplementedError`.

        The real implementation lands in Epic 02. Will archive the
        current conversation, optionally create a replacement, and reset
        per-session caches. Maps to engine.ts ``handleBeforeReset``
        (line 7415).

        Raises:
            NotImplementedError: Always at v0.
        """
        raise NotImplementedError("on_session_reset lands in Epic 02 (engine skeleton)")

    # ABC §Tools (defaults are fine; explicit overrides for v0 clarity) -----

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return an empty tool list for v0.

        The 8 ``lcm_*`` tools (lcm_grep, lcm_describe, lcm_expand,
        lcm_synthesize_around, lcm_get_entity, lcm_search_entities,
        lcm_compact, lcm_conversation_scope) all land in Epic 06.
        Returning ``[]`` matches the ABC default but is explicit here so
        the contract is obvious.

        Returns:
            Empty list at v0.
        """
        return []

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Raise :class:`NotImplementedError` — no tools at v0.

        The ABC default returns a JSON error string; we override to
        raise instead so a stray dispatch (which should not happen since
        :meth:`get_tool_schemas` returns ``[]``) surfaces loudly. Real
        tool handling lands in Epic 06.

        Args:
            name: Tool name being dispatched.
            args: Tool arguments.
            **kwargs: May include ``messages`` per ABC contract.

        Raises:
            NotImplementedError: Always at v0.
        """
        raise NotImplementedError(f"handle_tool_call({name!r}, ...): tools land in Epic 06")
