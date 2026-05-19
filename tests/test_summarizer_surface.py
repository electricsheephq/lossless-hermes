"""Direct tests for the summarizer surface on :class:`LCMEngine` (#164 PR-2).

Issue [#164](https://github.com/electricsheephq/lossless-hermes/issues/164)
PR-2 builds the *summarizer surface* on :class:`~lossless_hermes.engine.LCMEngine`:
``on_session_start`` constructs a
:class:`~lossless_hermes.hermes_llm.HermesSummarizerDeps` (PR-1's shim)
and an :class:`~lossless_hermes.summarize.LcmSummarizer` from it, then
exposes three handles:

* ``engine.deps`` — the public :class:`~lossless_hermes.summarize.SummarizerDeps`;
* ``engine.summarize`` — the bound :class:`~lossless_hermes.summarize.LcmSummarizer.summarize`,
  a :data:`~lossless_hermes.compaction.SummarizeFn`-shaped callable;
* ``engine._summarizer`` — the underlying :class:`LcmSummarizer` object
  (the ``build_llm_call`` factory reads ``.candidates`` off it).

This file is the direct coverage for that surface. What it pins:

* the three handles are ``None`` on a bare engine and on a post-
  ``on_session_end`` engine, populated between ``on_session_start`` and
  ``on_session_end``;
* ``engine.deps`` is a real :class:`SummarizerDeps`, ``engine.summarize``
  is callable;
* ``on_session_start`` is idempotent — a second call on an already-open
  DB does NOT rebuild the summarizer (the surface is config-scoped, not
  session-scoped);
* ``engine.summarize`` round-trips: with a fake ``complete()`` injected
  via a :class:`HermesSummarizerDeps` subclass and a configured summary
  model, ``engine.summarize("text")`` returns the canned summary;
* ``/lcm doctor apply`` no longer hits its unconditional "unavailable"
  arm — ``apply_scoped_doctor_repair`` resolves the engine's ``deps``
  into a real :class:`LcmSummarizer` (the doctor-apply confirmation the
  #164 plan's PR-2 §4 calls for).

Platform note
-------------

Every test here calls ``on_session_start``, which needs a full
``open_lcm_db`` connection (sqlite-vec loads via
``enable_load_extension``). Apple's system CPython ships without
``--enable-loadable-sqlite-extensions`` and the engine hard-raises at
construction — so the whole module carries the
``enable_load_extension`` skip marker, mirroring
``tests/test_dispatch_registry_coverage.py`` and
``tests/tools/test_compact_adapter.py``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator, Mapping

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import LCMEngine
from lossless_hermes.hermes_llm import HermesSummarizerDeps
from lossless_hermes.summarize import LcmSummarizer, SummarizerDeps

# ---------------------------------------------------------------------------
# Skip marker — Apple system Python lacks enable_load_extension
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load and "
        "the engine hard-raises at construction. Every test here calls "
        "on_session_start, so the whole module skips on such a build."
    ),
)


# ===========================================================================
# Test doubles
# ===========================================================================


class _FakeCompleteDeps(HermesSummarizerDeps):
    """A :class:`HermesSummarizerDeps` whose ``complete`` is a fake.

    The real :meth:`HermesSummarizerDeps.complete` lazy-imports Hermes's
    ``call_llm`` — unavailable in the test/CI env. This subclass
    overrides only ``complete`` with a deterministic fake that returns
    the cascade-required *block-list* envelope
    (``{"content": [{"type": "text", "text": ...}]}``) so
    ``engine.summarize`` round-trips without a real LLM. ``get_api_key``
    / ``is_runtime_managed_auth_provider`` are inherited unchanged — the
    genuine shim behaviour.
    """

    #: Canned summary the fake ``complete`` returns.
    SUMMARY_TEXT = "FAKE-SUMMARY: condensed rollup of the input."

    def complete(
        self,
        *,
        provider: str,
        model: str,
        api_key: str | None,
        system: str,
        user_prompt: str,
        max_tokens: int,
        reasoning: str | None = None,
        skip_model_auth: bool = False,
        timeout_ms: int,
    ) -> Mapping[str, Any]:
        """Return a canned block-list envelope (no real LLM call)."""
        del (
            provider,
            model,
            api_key,
            system,
            user_prompt,
            max_tokens,
            reasoning,
            skip_model_auth,
            timeout_ms,
        )
        return {"content": [{"type": "text", "text": self.SUMMARY_TEXT}]}


class _FakeDepsEngine(LCMEngine):
    """An :class:`LCMEngine` whose ``on_session_start`` uses fake deps.

    ``on_session_start`` builds a real :class:`HermesSummarizerDeps`,
    whose ``complete`` would call Hermes's ``call_llm``. Tests that need
    ``engine.summarize`` to actually *run* override the engine to
    substitute :class:`_FakeCompleteDeps` instead — keeping every other
    line of the lifecycle body (the ``LcmSummarizer`` construction, the
    handle assignment, idempotence) genuine.

    This mirrors the ``_ScriptedCompactEngine`` pattern in
    ``tests/tools/test_compact_adapter.py``: subclass the engine to
    swap one collaborator, exercise the real surrounding wiring.
    """

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        super().on_session_start(session_id, **kwargs)
        # Only swap the deps when the real surface was just built (the
        # non-re-entrant path). A re-entrant call leaves the existing
        # fake-backed summarizer in place — which is exactly what the
        # idempotence test asserts.
        if self.deps is not None and not isinstance(self.deps, _FakeCompleteDeps):
            fake_deps = _FakeCompleteDeps()
            self.deps = fake_deps
            summarizer = LcmSummarizer(
                deps=fake_deps,
                config=self.config,
                provider_hint=self.config.summary_provider or None,
                model_hint=self.config.summary_model or None,
            )
            self._summarizer = summarizer
            self.summarize = summarizer.summarize


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def started_engine(tmp_home: Path) -> Iterator[LCMEngine]:
    """An :class:`LCMEngine` with ``on_session_start`` run — default config.

    Default :class:`LcmConfig` — no ``summary_model`` configured, so the
    summarizer's candidate chain is empty. The summarizer *object* is
    still built (``engine.deps`` / ``engine.summarize`` populated); only
    a ``summarize()`` *call* would raise. Used by the presence /
    idempotence tests, which never call ``summarize``.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("surface-test-session")
    try:
        yield eng
    finally:
        eng.on_session_end("surface-test-session", [])


# ===========================================================================
# Presence — the three handles populate at on_session_start
# ===========================================================================


def test_bare_engine_has_no_summarizer_surface() -> None:
    """A bare :class:`LCMEngine` (no ``on_session_start``) has the surface ``None``.

    Per ADR-001 the constructor does heavy init nowhere — the summarizer
    surface, like the stores, is ``None`` until ``on_session_start``.
    """
    eng = LCMEngine(config=LcmConfig())
    assert eng.deps is None
    assert eng.summarize is None
    assert eng._summarizer is None


def test_on_session_start_builds_the_summarizer_surface(
    started_engine: LCMEngine,
) -> None:
    """After ``on_session_start`` the three handles are populated.

    ``engine.deps`` is a :class:`SummarizerDeps` (concretely a
    :class:`HermesSummarizerDeps`), ``engine.summarize`` is callable, and
    ``engine._summarizer`` is the :class:`LcmSummarizer` object.
    """
    assert started_engine.deps is not None
    assert isinstance(started_engine.deps, HermesSummarizerDeps)
    assert started_engine.summarize is not None
    assert callable(started_engine.summarize)
    assert started_engine._summarizer is not None
    assert isinstance(started_engine._summarizer, LcmSummarizer)
    # engine.summarize is the bound LcmSummarizer.summarize — the
    # SummarizeFn-shaped callable downstream consumers expect.
    assert started_engine.summarize == started_engine._summarizer.summarize


def test_engine_deps_satisfies_summarizer_deps_protocol(
    started_engine: LCMEngine,
) -> None:
    """``engine.deps`` structurally satisfies the :class:`SummarizerDeps` Protocol.

    The same conformance check ``hermes_llm.py``'s own tests use — a
    typed binding to :class:`SummarizerDeps` plus a probe of each
    Protocol member. :class:`SummarizerDeps` declares methods, so it is
    not ``runtime_checkable``; this asserts the surface directly.
    """
    deps = started_engine.deps
    assert deps is not None
    bound: SummarizerDeps = deps  # static + structural: deps IS a SummarizerDeps
    assert callable(bound.complete)
    assert callable(bound.get_api_key)
    assert callable(bound.is_runtime_managed_auth_provider)


def test_on_session_end_clears_the_summarizer_surface(tmp_home: Path) -> None:
    """After ``on_session_end`` the three handles are ``None`` again.

    Symmetric with the store null-out — a post-close engine reports the
    pre-first-``on_session_start`` shape, so ``getattr(engine, "deps")``
    probes (``commands/doctor.py``) see ``None``.
    """
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("end-test-session")
    assert eng.deps is not None  # built
    eng.on_session_end("end-test-session", [])
    assert eng.deps is None
    assert eng.summarize is None
    assert eng._summarizer is None


# ===========================================================================
# Idempotence — the surface is config-scoped, not session-scoped
# ===========================================================================


def test_on_session_start_is_idempotent_for_the_summarizer(
    started_engine: LCMEngine,
) -> None:
    """A re-entrant ``on_session_start`` does NOT rebuild the summarizer.

    The summarizer surface is config-scoped (built once per DB-open),
    not session-scoped. ``on_session_start`` for a *new* session on an
    already-open DB returns early at the ``self._db is not None`` guard
    and must NOT churn the summarizer — the object identity is stable.
    """
    deps_before = started_engine.deps
    summarizer_before = started_engine._summarizer
    summarize_before = started_engine.summarize

    # Second on_session_start — a new session id, same already-open DB.
    started_engine.on_session_start("surface-test-session-2")

    # Identity stable — not rebuilt.
    assert started_engine.deps is deps_before
    assert started_engine._summarizer is summarizer_before
    assert started_engine.summarize is summarize_before
    # The re-entrant call DID still update current_session_id (the one
    # thing the re-entrant path is documented to do).
    assert started_engine.current_session_id == "surface-test-session-2"


# ===========================================================================
# Round-trip — engine.summarize actually summarizes
# ===========================================================================


def test_engine_summarize_round_trips_with_a_fake_complete(
    tmp_home: Path,
) -> None:
    """``engine.summarize("text")`` returns the canned summary.

    With a configured summary model (so the candidate chain is non-empty)
    and a :class:`_FakeCompleteDeps` injected, ``engine.summarize`` runs
    the real :class:`LcmSummarizer` cascade end-to-end against the fake
    ``complete`` and returns its canned block-list envelope's text. This
    proves the surface is wired correctly — the bound method is the live
    summarizer, not a stub.
    """
    config = LcmConfig(summary_provider="test-provider", summary_model="test-model")
    eng = _FakeDepsEngine(hermes_home=tmp_home / ".hermes", config=config)
    eng.on_session_start("round-trip-session")
    try:
        assert eng.summarize is not None
        result = eng.summarize("Some input text that needs summarizing.")
        assert result == _FakeCompleteDeps.SUMMARY_TEXT
    finally:
        eng.on_session_end("round-trip-session", [])


def test_engine_summarizer_has_resolved_candidates_when_configured(
    tmp_home: Path,
) -> None:
    """A configured summary model yields a non-empty candidate chain.

    ``engine._summarizer.candidates`` is what the
    ``lcm_synthesize_around`` ``build_llm_call`` factory reads for the
    Wave-12 F8 audit-honest model name. With ``summary_provider`` /
    ``summary_model`` set, the primary candidate's model echoes the
    config.
    """
    config = LcmConfig(summary_provider="test-provider", summary_model="test-model")
    eng = _FakeDepsEngine(hermes_home=tmp_home / ".hermes", config=config)
    eng.on_session_start("candidates-session")
    try:
        summarizer = eng._summarizer
        assert summarizer is not None
        assert summarizer.candidates, "configured summary model must resolve a candidate"
        assert summarizer.candidates[0].model == "test-model"
    finally:
        eng.on_session_end("candidates-session", [])


# ===========================================================================
# /lcm doctor apply — the "unavailable" arm no longer fires (surface side-effect)
# ===========================================================================
#
# ``commands/doctor.py`` (the ``/lcm doctor apply`` handler) probes the
# engine for a summarizer at ``doctor.py:261-262``::
#
#     deps = getattr(engine, "deps", None)
#     summarize = getattr(engine, "summarize", None)
#     ...
#     apply_scoped_doctor_repair(..., deps=deps,
#                                summarize=summarize if callable(summarize) else None)
#
# and ``apply_scoped_doctor_repair`` reports ``kind="unavailable"`` iff
# ``_resolve_doctor_apply_summarize(...)`` returns ``None``. Before #164
# PR-2 both probes returned ``None`` (the engine had no ``deps`` /
# ``summarize`` attribute at all), so doctor-apply's "unavailable" arm
# fired *unconditionally*. #164 PR-2's surface populates both — the
# tests below confirm doctor-apply no longer hits that arm, without
# regressing it.


def test_doctor_apply_resolver_returns_a_summarizer_from_the_engine_surface(
    tmp_home: Path,
) -> None:
    """``_resolve_doctor_apply_summarize`` resolves the engine's surface → not ``None``.

    ``_resolve_doctor_apply_summarize`` is the exact function whose
    return value decides doctor-apply's "unavailable" arm — a ``None``
    return is "unavailable", a callable return is "the repair runs".
    Driven with the engine's real #164 PR-2 handles exactly as
    ``commands/doctor.py:261-289`` passes them (``deps=engine.deps``,
    ``summarize=engine.summarize`` — which is callable), it returns a
    usable callable, NOT ``None``. So doctor-apply no longer reports
    "unavailable": the previously-unconditional arm is now skipped.
    """
    from lossless_hermes.doctor.apply import _resolve_doctor_apply_summarize

    config = LcmConfig(summary_provider="test-provider", summary_model="test-model")
    eng = _FakeDepsEngine(hermes_home=tmp_home / ".hermes", config=config)
    eng.on_session_start("doctor-resolver-session")
    try:
        # Mirror commands/doctor.py:261-262's exact getattr probe.
        deps = getattr(eng, "deps", None)
        summarize = getattr(eng, "summarize", None)
        assert deps is not None, "engine.deps probe must resolve post-PR-2"
        assert callable(summarize), "engine.summarize probe must resolve to a callable"

        resolved = _resolve_doctor_apply_summarize(
            config=eng.config,
            deps=deps,
            # commands/doctor.py:287 passes ``summarize if callable(...) else None``.
            summarize=summarize if callable(summarize) else None,
            runtime_config=None,
        )
        assert resolved is not None, (
            "_resolve_doctor_apply_summarize must return a usable summarizer "
            "from the engine's surface — a None return is the 'unavailable' arm."
        )
        assert callable(resolved)
    finally:
        eng.on_session_end("doctor-resolver-session", [])


def test_doctor_apply_resolver_unavailable_on_a_pre_init_engine() -> None:
    """On a bare engine the resolver returns ``None`` — the "unavailable" arm.

    The contrapositive: #164 PR-2 does NOT make doctor-apply always
    "available". A bare :class:`LCMEngine` (no ``on_session_start``) has
    ``deps`` / ``summarize`` both ``None`` — exactly the pre-init shape
    ``commands/doctor.py`` already guards against — and
    ``_resolve_doctor_apply_summarize`` correctly returns ``None``. The
    arm is genuinely *conditional* on the surface being constructed, not
    dead and not always-on. (The doctor-apply command also rejects a
    DB-less engine far earlier — this pins the resolver leg itself.)
    """
    from lossless_hermes.doctor.apply import _resolve_doctor_apply_summarize

    eng = LCMEngine(config=LcmConfig())
    deps = getattr(eng, "deps", None)
    summarize = getattr(eng, "summarize", None)
    assert deps is None
    assert summarize is None

    resolved = _resolve_doctor_apply_summarize(
        config=eng.config,
        deps=deps,
        summarize=summarize if callable(summarize) else None,
        runtime_config=None,
    )
    assert resolved is None, (
        "A pre-init engine has no summarizer surface — the resolver must "
        "return None (doctor-apply's 'unavailable' arm)."
    )
