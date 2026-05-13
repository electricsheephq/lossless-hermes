"""Tests for :meth:`LCMEngine.update_from_response` cache-aware token tracking.

Covers issue 02-04 (token tracking extensions) — the extension of the
existing ``update_from_response`` to capture ``cache_read_tokens`` /
``cache_write_tokens`` across three usage shapes:

1. **OpenAI Chat** — ``prompt_tokens``, ``completion_tokens``,
   ``total_tokens``. No cache fields native to this shape.
2. **Anthropic native** — ``input_tokens``, ``output_tokens``,
   ``cache_creation_input_tokens``, ``cache_read_input_tokens``.
3. **OpenAI Responses (Codex)** — ``prompt_tokens_details.cached_tokens``.

The Hermes-normalized keys ``cache_read_tokens`` / ``cache_write_tokens``
(per ADR-015 patch #4) take precedence when present — that's the wire
shape Hermes will forward once the upstream patch lands.

Per ADR-015 patch #4 §"Without it": when no cache fields are present,
``cache_aware`` stays ``False`` and the cache-aware deferral gate
(Epic 04) degrades to a conservative always-compact-when-over-threshold
policy. No crash, no warning storm — silent graceful degradation.

This file is paired with the 00-06/02-01 regression tests in
``tests/test_engine_noop.py``; the prompt/completion/total assertions
there must still pass.
"""

from __future__ import annotations

from lossless_hermes.engine import LCMEngine


# ---------------------------------------------------------------------------
# Baseline state (cache fields default to absent/zero)
# ---------------------------------------------------------------------------


def test_cache_fields_initialized_to_zero_and_absent() -> None:
    """Cache fields default to 0 + ``cache_aware = False`` until observed."""
    engine = LCMEngine()
    assert engine.last_cache_read_tokens == 0
    assert engine.last_cache_write_tokens == 0
    assert engine.cache_aware is False


# ---------------------------------------------------------------------------
# Shape 1: OpenAI Chat (no native cache fields)
# ---------------------------------------------------------------------------


def test_openai_chat_shape_stores_prompt_completion_total() -> None:
    """AC: OpenAI Chat usage stores prompt/completion/total correctly."""
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_tokens": 1200,
    })
    assert engine.last_prompt_tokens == 1000
    assert engine.last_completion_tokens == 200
    assert engine.last_total_tokens == 1200


def test_openai_chat_shape_no_cache_signal() -> None:
    """OpenAI Chat shape has no cache fields → ``cache_aware`` stays False."""
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_tokens": 1200,
    })
    assert engine.cache_aware is False
    assert engine.last_cache_read_tokens == 0
    assert engine.last_cache_write_tokens == 0


# ---------------------------------------------------------------------------
# Shape 2: Anthropic native
# ---------------------------------------------------------------------------


def test_anthropic_native_shape_stores_all_fields_including_cache() -> None:
    """AC: Anthropic-native shape captures input/output + cache_read/write.

    The native Anthropic ``usage`` payload uses
    ``cache_read_input_tokens`` and ``cache_creation_input_tokens``,
    distinct from the Hermes-normalized ``cache_read_tokens`` /
    ``cache_write_tokens``. The engine accepts both.
    """
    engine = LCMEngine()
    engine.update_from_response({
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 50,
    })
    assert engine.last_prompt_tokens == 500
    assert engine.last_completion_tokens == 100
    assert engine.last_total_tokens == 600
    assert engine.last_cache_read_tokens == 300
    assert engine.last_cache_write_tokens == 50
    assert engine.cache_aware is True


def test_anthropic_native_shape_only_cache_read() -> None:
    """Anthropic shape with only cache_read (no cache_write) still aware."""
    engine = LCMEngine()
    engine.update_from_response({
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_read_input_tokens": 200,
    })
    assert engine.last_cache_read_tokens == 200
    # cache_write absent → 0, but cache_aware is True because we saw a signal
    assert engine.last_cache_write_tokens == 0
    assert engine.cache_aware is True


def test_anthropic_native_shape_only_cache_write() -> None:
    """Anthropic shape with only cache_creation (no cache_read) still aware."""
    engine = LCMEngine()
    engine.update_from_response({
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_creation_input_tokens": 75,
    })
    assert engine.last_cache_read_tokens == 0
    assert engine.last_cache_write_tokens == 75
    assert engine.cache_aware is True


# ---------------------------------------------------------------------------
# Shape 3: OpenAI Responses (Codex)
# ---------------------------------------------------------------------------


def test_openai_responses_shape_extracts_cached_tokens() -> None:
    """AC: OpenAI Responses (Codex) shape extracts cached_tokens via nested dict.

    The Codex harness's Responses-API usage payload wraps the cache hit
    count under ``prompt_tokens_details``:
    ``{"cached_tokens": <int>}``. There is no cache-write counter in
    this shape — only the read side is reported.
    """
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 800,
        "completion_tokens": 150,
        "total_tokens": 950,
        "prompt_tokens_details": {"cached_tokens": 600},
    })
    assert engine.last_prompt_tokens == 800
    assert engine.last_completion_tokens == 150
    assert engine.last_total_tokens == 950
    assert engine.last_cache_read_tokens == 600
    assert engine.last_cache_write_tokens == 0  # Codex doesn't expose write
    assert engine.cache_aware is True


def test_openai_responses_shape_empty_details_is_no_signal() -> None:
    """If ``prompt_tokens_details`` lacks ``cached_tokens``, no cache signal."""
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 800,
        "completion_tokens": 150,
        "prompt_tokens_details": {},
    })
    assert engine.cache_aware is False
    assert engine.last_cache_read_tokens == 0


def test_openai_responses_shape_non_dict_details_ignored() -> None:
    """Malformed ``prompt_tokens_details`` (not a dict) doesn't crash."""
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 800,
        "completion_tokens": 150,
        "prompt_tokens_details": "garbage",
    })
    assert engine.cache_aware is False
    assert engine.last_cache_read_tokens == 0


# ---------------------------------------------------------------------------
# Hermes-normalized shape (post-ADR-015 patch #4)
# ---------------------------------------------------------------------------


def test_hermes_normalized_keys_take_precedence() -> None:
    """``cache_read_tokens`` / ``cache_write_tokens`` win over Anthropic-native.

    Once ADR-015 patch #4 lands, ``run_agent.py`` will forward usage
    dicts that include both the original Anthropic fields AND the
    normalized ``cache_read_tokens`` / ``cache_write_tokens`` keys.
    The normalized keys are authoritative; the engine should prefer
    them and ignore the native ones.
    """
    engine = LCMEngine()
    engine.update_from_response({
        "input_tokens": 500,
        "output_tokens": 100,
        # Hermes-normalized (post-patch #4) — these win
        "cache_read_tokens": 999,
        "cache_write_tokens": 111,
        # Anthropic-native — ignored when normalized fields present
        "cache_read_input_tokens": 1,
        "cache_creation_input_tokens": 2,
    })
    assert engine.last_cache_read_tokens == 999
    assert engine.last_cache_write_tokens == 111
    assert engine.cache_aware is True


def test_hermes_normalized_keys_alone() -> None:
    """The normalized keys alone (without provider-native) work fine."""
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cache_read_tokens": 400,
        "cache_write_tokens": 80,
    })
    assert engine.last_cache_read_tokens == 400
    assert engine.last_cache_write_tokens == 80
    assert engine.cache_aware is True


def test_hermes_normalized_zero_cache_read_still_aware() -> None:
    """Explicit ``cache_read_tokens: 0`` is a real signal — cache_aware = True.

    Zero is meaningful here: it means "we tried to use the cache and
    got no hit". That's different from "the field wasn't sent at all".
    The deferral gate cares about this distinction; ``cache_aware``
    must flip on observation of the key regardless of value.
    """
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    })
    assert engine.cache_aware is True
    assert engine.last_cache_read_tokens == 0
    assert engine.last_cache_write_tokens == 0


# ---------------------------------------------------------------------------
# Graceful degradation: missing fields don't crash
# ---------------------------------------------------------------------------


def test_missing_cache_fields_graceful_degradation() -> None:
    """AC: missing cache fields → ``cache_aware = False``; no crash."""
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    })
    assert engine.cache_aware is False
    assert engine.last_cache_read_tokens == 0
    assert engine.last_cache_write_tokens == 0
    # And the regular fields still work
    assert engine.last_prompt_tokens == 100


def test_empty_usage_does_not_crash() -> None:
    """Empty dict is the worst case — everything falls back to 0."""
    engine = LCMEngine()
    engine.update_from_response({})
    assert engine.last_prompt_tokens == 0
    assert engine.last_completion_tokens == 0
    assert engine.last_total_tokens == 0
    assert engine.last_cache_read_tokens == 0
    assert engine.last_cache_write_tokens == 0
    assert engine.cache_aware is False


def test_cache_aware_reflects_current_call_not_sticky() -> None:
    """``cache_aware`` is per-turn, not sticky across calls.

    Per the 02-04 spec acceptance criterion ("Missing cache fields →
    cache_aware = False"), the flag is a current-call signal: it tells
    Epic 04's deferral gate whether the MOST RECENT turn had cache
    data. A turn without cache fields must flip the flag back to False
    even if an earlier turn reported a cache hit — otherwise the gate
    would over-trust stale data and skip compaction inappropriately.

    The per-turn counters reset to 0 alongside the flag for the same
    reason: stale ``last_cache_read_tokens`` from a prior turn must
    not bleed into the current turn's gate decision.
    """
    engine = LCMEngine()
    engine.update_from_response({
        "input_tokens": 500,
        "output_tokens": 100,
        "cache_read_input_tokens": 300,
    })
    assert engine.cache_aware is True
    assert engine.last_cache_read_tokens == 300

    # Subsequent call without cache fields — flag goes False, counters reset
    engine.update_from_response({
        "prompt_tokens": 1000,
        "completion_tokens": 200,
    })
    assert engine.cache_aware is False
    assert engine.last_cache_read_tokens == 0
    assert engine.last_cache_write_tokens == 0


# ---------------------------------------------------------------------------
# 00-06 regression — existing tests must still pass via this path
# ---------------------------------------------------------------------------


def test_regression_records_tokens_openai_style() -> None:
    """00-06 baseline: OpenAI-style keys store correctly."""
    engine = LCMEngine()
    engine.update_from_response({
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
    })
    assert engine.last_prompt_tokens == 100
    assert engine.last_completion_tokens == 50
    assert engine.last_total_tokens == 150


def test_regression_tolerates_anthropic_legacy_keys() -> None:
    """00-06 baseline: input_tokens/output_tokens still work."""
    engine = LCMEngine()
    engine.update_from_response({"input_tokens": 200, "output_tokens": 75})
    assert engine.last_prompt_tokens == 200
    assert engine.last_completion_tokens == 75
    assert engine.last_total_tokens == 275


def test_regression_computes_total_when_absent() -> None:
    """00-06 baseline: total computed from prompt + completion."""
    engine = LCMEngine()
    engine.update_from_response({"prompt_tokens": 30, "completion_tokens": 20})
    assert engine.last_total_tokens == 50
