"""Tests for issue 02-02 state-field additions + 02-06 compress refinement.

Covers the remaining state-field cleanup from
``epics/02-engine-skeleton/02-02-engine-state.md`` (everything that
02-01/04/05/08/09 did NOT already declare) plus the 02-06 refinement of
``compress`` (``compression_count`` increment + debug log).

What this file does NOT cover (already covered elsewhere):

* The full mixin-composition MRO / shell-class invariants — see
  ``tests/test_engine_skeleton.py``.
* The 02-05 anti-thrashing gate on ``should_compress`` — see
  ``tests/test_should_compress.py``.
* The 02-04 ``update_from_response`` cache-aware bookkeeping — see
  ``tests/test_engine_skeleton.py`` and ``tests/test_engine_noop.py``.
* The 02-09 circuit-breaker state machine — see
  ``tests/test_circuit_breaker.py``.

The new fields (per 02-02 spec table):

* :attr:`LCMEngine.info` — :class:`ContextEngineInfo` identity record.
* :attr:`LCMEngine.migrated` — set to True by 02-03's on_session_start.
* :attr:`LCMEngine.fts5_available` — feature flag updated by 02-03.
* :attr:`LCMEngine.ignore_session_patterns` — compiled regex list.
* :attr:`LCMEngine.stateless_session_patterns` — compiled regex list.
* :attr:`LCMEngine._previous_assembled_messages_by_conversation` —
  Epic 03 assembly state.
* :attr:`LCMEngine._stable_orphan_stripping_ordinals_by_conversation` —
  Epic 03 assembly boundary.
* :attr:`LCMEngine._cache_context_unknown_logged` — per-process
  dedupe for cache-unknown log warnings.

The 02-06 surface tested here:

* :attr:`LCMEngine.compression_count` increments by 1 per
  :meth:`LCMEngine.compress` call.

See:

* ``epics/02-engine-skeleton/02-02-engine-state.md`` — canonical AC.
* ``epics/02-engine-skeleton/02-06-noop-compress.md`` — canonical AC.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
"""

from __future__ import annotations

import logging
import re
from dataclasses import is_dataclass

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.engine import ContextEngineInfo, LCMEngine


# ---------------------------------------------------------------------------
# ContextEngineInfo dataclass shape
# ---------------------------------------------------------------------------


def test_context_engine_info_is_frozen_dataclass() -> None:
    """02-02: ``ContextEngineInfo`` is a frozen dataclass.

    Frozen so the identity record cannot be mutated after the engine
    publishes it to Hermes; ``dataclasses.replace`` is the sanctioned
    mutation surface (Epic 04 uses it in the compaction-failure path
    to set ``owns_compaction=False``).
    """
    assert is_dataclass(ContextEngineInfo)
    info = ContextEngineInfo()
    # Frozen → assigning to a field raises FrozenInstanceError.
    try:
        info.name = "other"  # type: ignore[misc]
    except Exception as e:
        # Either FrozenInstanceError or AttributeError (slots) is fine.
        assert "frozen" in str(type(e).__name__).lower() or "attribute" in str(e).lower()
    else:
        raise AssertionError("ContextEngineInfo must be frozen")


def test_context_engine_info_default_values() -> None:
    """02-02: default values match the spec table.

    ``name="lcm"`` (ADR-001 selector string), ``version="0.1.2"``
    (bumped from 0.1.1 by the v0.1.2 durability patch — tracks
    ``pyproject.toml``), ``owns_compaction=True`` (degrades to False only
    on migration failure — Epic 04 wires that path).
    """
    info = ContextEngineInfo()
    assert info.name == "lcm"
    assert info.version == "0.1.2"
    assert info.owns_compaction is True


# ---------------------------------------------------------------------------
# 02-02: info attribute on the engine instance
# ---------------------------------------------------------------------------


def test_engine_info_is_context_engine_info() -> None:
    """02-02: ``engine.info`` is a :class:`ContextEngineInfo` with the
    expected default values.

    Per the spec table row 1: ``info: ContextEngineInfo`` — "Identity +
    ``owns_compaction`` flag (true unless migration failed)". At
    construction time migration has not run, but we still default to
    ``True`` — 02-03's lifecycle body re-checks and Epic 04's failure
    path may demote.
    """
    engine = LCMEngine()
    assert isinstance(engine.info, ContextEngineInfo)
    assert engine.info.name == "lcm"
    assert engine.info.version == "0.1.2"
    assert engine.info.owns_compaction is True


# ---------------------------------------------------------------------------
# 02-02: migrated flag default
# ---------------------------------------------------------------------------


def test_migrated_defaults_to_false() -> None:
    """02-02: ``engine.migrated`` is ``False`` at construction.

    Per ADR-001, heavy init defers to ``on_session_start`` — at
    construction time migrations have not run, so the flag must be
    False. 02-03's lifecycle body flips this to True after
    ``run_lcm_migrations`` succeeds.
    """
    engine = LCMEngine()
    assert engine.migrated is False
    assert isinstance(engine.migrated, bool)


# ---------------------------------------------------------------------------
# 02-02: fts5_available default
# ---------------------------------------------------------------------------


def test_fts5_available_defaults_to_true() -> None:
    """02-02: ``engine.fts5_available`` is ``True`` at construction.

    Optimistic default — the real probe runs in 02-03's lifecycle body
    via ``get_lcm_db_features(conn).fts5_available`` and overwrites
    this before any store reads run. Default ``True`` is fine for
    Epic 02 because no store reads happen until 02-03 has run.
    """
    engine = LCMEngine()
    assert engine.fts5_available is True
    assert isinstance(engine.fts5_available, bool)


# ---------------------------------------------------------------------------
# 02-02: pattern compilation from LcmConfig
# ---------------------------------------------------------------------------


def test_ignore_session_patterns_empty_default() -> None:
    """02-02: ``engine.ignore_session_patterns`` is an empty list when
    ``config.ignore_session_patterns`` is empty (the LcmConfig default).
    """
    engine = LCMEngine()
    assert engine.ignore_session_patterns == []
    assert isinstance(engine.ignore_session_patterns, list)


def test_stateless_session_patterns_empty_default() -> None:
    """02-02: ``engine.stateless_session_patterns`` is an empty list when
    ``config.stateless_session_patterns`` is empty (the LcmConfig default).
    """
    engine = LCMEngine()
    assert engine.stateless_session_patterns == []
    assert isinstance(engine.stateless_session_patterns, list)


def test_ignore_session_patterns_compiled_from_config_strings() -> None:
    """02-02: ``engine.ignore_session_patterns`` is a list of compiled
    regex patterns, sourced from the LcmConfig string list.

    Per the spec table: "Compiled from ``config.ignore_session_patterns``".
    The runtime type is :class:`re.Pattern`; the source type on the
    config is :class:`list[str]`.
    """
    cfg = LcmConfig(ignore_session_patterns=[r"^test-.*", r"^ci-bench-\d+$"])
    engine = LCMEngine(config=cfg)

    assert len(engine.ignore_session_patterns) == 2
    for pat in engine.ignore_session_patterns:
        assert isinstance(pat, re.Pattern)

    # Spot-check the compiled patterns are usable.
    assert engine.ignore_session_patterns[0].match("test-foo") is not None
    assert engine.ignore_session_patterns[0].match("prod-foo") is None
    assert engine.ignore_session_patterns[1].match("ci-bench-42") is not None
    assert engine.ignore_session_patterns[1].match("ci-bench-abc") is None


def test_stateless_session_patterns_compiled_from_config_strings() -> None:
    """02-02: ``engine.stateless_session_patterns`` is a list of compiled
    regex patterns, sourced from the LcmConfig string list.

    Mirror of ``ignore_session_patterns`` — same source-shape, same
    target-shape, different downstream use (bypass DB writes but keep
    observability).
    """
    cfg = LcmConfig(stateless_session_patterns=[r"^stateless-.*"])
    engine = LCMEngine(config=cfg)

    assert len(engine.stateless_session_patterns) == 1
    assert isinstance(engine.stateless_session_patterns[0], re.Pattern)
    assert engine.stateless_session_patterns[0].match("stateless-1") is not None
    assert engine.stateless_session_patterns[0].match("regular-1") is None


# ---------------------------------------------------------------------------
# 02-02: assembly-state dicts (Epic 03 consumers)
# ---------------------------------------------------------------------------


def test_previous_assembled_messages_by_conversation_empty_dict() -> None:
    """02-02: ``engine._previous_assembled_messages_by_conversation`` is
    an empty dict at construction.

    Used by :class:`_AssembleMixin` in Epic 03 for prefix-stability
    diagnostics. At 02-02 we just confirm the field exists with the
    right empty default.
    """
    engine = LCMEngine()
    assert engine._previous_assembled_messages_by_conversation == {}
    assert isinstance(engine._previous_assembled_messages_by_conversation, dict)


def test_stable_orphan_stripping_ordinals_by_conversation_empty_dict() -> None:
    """02-02: ``engine._stable_orphan_stripping_ordinals_by_conversation``
    is an empty dict at construction.

    Used by :class:`_AssembleMixin` in Epic 03 for stable boundary
    tracking. At 02-02 we just confirm the field exists with the
    right empty default.
    """
    engine = LCMEngine()
    assert engine._stable_orphan_stripping_ordinals_by_conversation == {}
    assert isinstance(engine._stable_orphan_stripping_ordinals_by_conversation, dict)


# ---------------------------------------------------------------------------
# 02-02: cache-context log dedupe set
# ---------------------------------------------------------------------------


def test_cache_context_unknown_logged_empty_set() -> None:
    """02-02: ``engine._cache_context_unknown_logged`` is an empty set at
    construction.

    Per-process dedupe — keyed by conversation id; an entry's presence
    means the cache-context-unknown warning already fired for that
    conversation this process. At 02-02 we just confirm the field
    exists with the right empty default and the right container type.
    """
    engine = LCMEngine()
    assert engine._cache_context_unknown_logged == set()
    assert isinstance(engine._cache_context_unknown_logged, set)


# ---------------------------------------------------------------------------
# 02-06: compress increments compression_count + debug-logs
# ---------------------------------------------------------------------------


def test_compress_increments_compression_count_by_one() -> None:
    """02-06: each ``compress`` call increments ``compression_count`` by 1.

    Per the spec AC bullet: "After ``compress`` returns,
    ``engine.compression_count`` is incremented by 1".

    Hermes's ``run_agent.py:10377`` reads this for the per-turn display
    — a "called but did nothing" still counts as one compress
    invocation from the host's perspective.
    """
    engine = LCMEngine()
    assert engine.compression_count == 0

    engine.compress([{"role": "user", "content": "hi"}])
    assert engine.compression_count == 1

    engine.compress([{"role": "user", "content": "hi"}])
    assert engine.compression_count == 2

    engine.compress([{"role": "user", "content": "hi"}])
    assert engine.compression_count == 3


def test_compress_increments_compression_count_for_empty_messages() -> None:
    """02-06: empty messages list still triggers the count increment.

    The increment is per-call, not per-message — Hermes called the
    compress entry point, so the counter goes up. Epic 04 may revisit
    if real compactions should increment a separate counter from no-op
    skipped ones (per the spec §Confidence note).
    """
    engine = LCMEngine()
    engine.compress([])
    assert engine.compression_count == 1


def test_compress_emits_debug_log(caplog: object) -> None:
    """02-06: ``compress`` emits a DEBUG-level log breadcrumb.

    The log line names the message count, ``current_tokens``, and
    ``focus_topic`` so an operator scanning logs can see the no-op
    fire while Epic 04 is still in flight.
    """
    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)
    engine = LCMEngine()
    with caplog.at_level(logging.DEBUG, logger="lossless_hermes.engine.compact"):
        engine.compress(
            [{"role": "user", "content": "hi"}],
            current_tokens=12345,
            focus_topic="quantum",
        )
    # Find the relevant log record.
    matches = [
        rec
        for rec in caplog.records
        if rec.name == "lossless_hermes.engine.compact"
        and "compress called (no-op)" in rec.getMessage()
    ]
    assert len(matches) == 1, f"expected one debug log; got {len(matches)}"
    msg = matches[0].getMessage()
    assert "1 messages" in msg
    assert "12345" in msg
    assert "quantum" in msg


def test_compress_returns_messages_unchanged_after_refinement() -> None:
    """02-06 regression: the 02-05 passthrough behavior is preserved.

    The 02-06 refinement adds counter increment + debug log; it must
    NOT alter the returned message list. Identity (``is``) is the
    strongest contract here — 02-05 already asserts ``==``.
    """
    engine = LCMEngine()
    msgs: list[dict] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    result = engine.compress(msgs, current_tokens=100, focus_topic="topic")
    assert result is msgs
