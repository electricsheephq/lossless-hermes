"""Per-session token-state cache for LCM tools.

Ports ``lossless-claw/src/plugin/token-state.ts`` (LCM commit ``1f07fbd``
on branch ``pr-613``, 273 LOC) to Python. The cache feeds the
:func:`needs_compact_gate.run_with_token_gate` middleware so it can
estimate ``(current + result) / budget`` and refuse to dispatch when the
projected ratio exceeds :data:`needs_compact_gate.REFUSAL_THRESHOLD`.

Architecture
------------

Hermes's :class:`ContextEngine` exposes :meth:`update_from_response`
already — it's invoked from ``run_agent.py`` after every API response with
the ``usage`` dict from the provider. This module bridges that hook into a
per-session-key snapshot the token-gate middleware can consult.

Two write paths feed the cache:

1. :func:`record_llm_output` — ground-truth anchor. The engine's
   :meth:`LCMEngine.update_from_response` calls this on every LLM
   response to capture the ``input + cacheRead + cacheWrite`` figure
   the provider reported. This is the same composition LCM uses
   (TS engine.ts:1262-1266); ``output`` tokens are the LLM response, not
   part of the context budget. The :attr:`TokenSnapshot.last_update_source`
   becomes ``"llm_output"``.

2. :func:`accumulate_tool_result_tokens` — additive estimate. Each tool's
   ``handle_tool_call`` epilogue calls :func:`tap_result_for_token_accounting`
   to add the size of the result it just emitted. This handles the
   parallel-tool-call case where the LLM emits multiple tool calls in one
   response — they all run between the same two ``llm_output`` events,
   so without this the second+ tool sees stale state. Drift is bounded
   per iteration and resets on the next ``llm_output``.

Both writes route through a single :data:`_tokens_by_session` dict keyed
by session-key. The :class:`threading.Lock` makes concurrent access safe
under Python's asyncio + thread executors.

Failure mode: one-iteration lag
-------------------------------

The very first tool batch of a turn sees no cached value — ``llm_output``
fires AFTER each LLM response, so the first tool call of the very first
turn precedes the first anchor. Tools must tolerate ``None`` /
empty-snapshot from :func:`get_runtime_context` and skip token-aware logic
in that case.

In practice the first iteration of a turn always has ``llm_output`` fire
before any tool runs (Hermes drives one LLM call -> tool dispatch loop),
so this is rarely visible — but the contract is "missing telemetry =
skip the gate", and the gate evaluation function (:func:`evaluate_needs_compact_gate`)
honors that.

Drift mitigation
----------------

The per-tool additive update is an ESTIMATE (``len(result_text) // 4``).
When the next ``llm_output`` fires the cache snaps back to ground truth.
Per-iteration drift is bounded by the iteration's tool batch size;
cross-iteration drift is reset on each LLM response.

Wave-12 audit: post-compact reset
---------------------------------

After a successful ``lcm_compact`` the engine's actual context drops
dramatically (e.g. 184K -> 70K). The cache, however, still carries the
pre-compact value until the NEXT ``llm_output`` event fires. Without
:func:`note_successful_compact`, the very next wrapped tool's
:func:`evaluate_needs_compact_gate` runs against the stale 184K and
refuses spuriously — exactly what compact was meant to prevent.

Strategy: clear the cache entry on successful compact. The next wrapped
tool call sees no snapshot -> gate bypasses (its documented behavior
when telemetry is missing) -> tool runs ->
:func:`tap_result_for_token_accounting` recreates the entry with the
size of the new result (small). Subsequent calls accumulate normally;
on the next ``llm_output`` we snap back to ground truth.

References
----------

* TS source: ``lossless-claw/src/plugin/token-state.ts``.
* Porting guide: ``docs/porting-guides/tools.md`` lines 599–610 plus the
  per-tool estimator subsections.
* Issue spec: ``epics/06-tools/06-03-runwithtokengate-middleware.md``.
* ADR-029 Wave-12 row for F5 (gate is middleware) and W2A1 (post-compact
  reset).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Final, Literal

#: Source attribution for the last cache write. ``llm_output`` is ground
#: truth from the provider; ``tool-self-report`` is the additive estimate
#: from a tool's tap. Stored on :class:`TokenSnapshot` so debugging hooks
#: can distinguish "anchored" from "estimated" state.
UpdateSource = Literal["llm_output", "tool-self-report"]


@dataclass(slots=True)
class TokenSnapshot:
    """Per-session token-state snapshot.

    All fields are integer counts of tokens (estimated tokens for
    ``tool-self-report`` writes; ground-truth from the provider for
    ``llm_output`` writes). Timestamps are float-seconds since epoch
    (``time.time()``), matching Python's stdlib monotonic-ish convention.

    Mirrors ``TokenSnapshot`` in ``token-state.ts`` lines 61–72.

    Attributes:
        current_token_count: ``input + cacheRead + cacheWrite`` from the
            last observed model call OR accumulated tool result sizes.
            Used by :func:`needs_compact_gate.evaluate_needs_compact_gate`
            in the numerator of ``(current + estimate) / budget``.
        token_budget: Active model's effective context budget. ``None``
            when not derivable (engine couldn't infer from the model
            string); gate bypasses in that case.
        anchor_at: ``time.time()`` of the last ``llm_output`` that
            anchored the value. Useful for debugging cache staleness.
        last_update_at: ``time.time()`` of the most recent write
            (anchor or tap). Updated on every write.
        last_update_source: Where the latest write came from.
            ``"llm_output"`` = ground truth; ``"tool-self-report"`` =
            estimate from a tool's tap.
    """

    current_token_count: int
    token_budget: int | None
    anchor_at: float
    last_update_at: float
    last_update_source: UpdateSource


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
#
# Per-session cache. Keyed by session-key (the cross-conversation identity
# that Hermes plumbs through ``kwargs["session_key"]`` when present).
# Empty session-keys are skipped at the write site.

_tokens_by_session: dict[str, TokenSnapshot] = {}

# Lock for concurrent access. Hermes runs tool dispatch on the
# calling-thread of ``run_agent`` but ``llm_output`` may fire from a
# different thread depending on how the async-bridge resolves. The lock
# is fine-grained per-call; no hot-path concern.
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# record_llm_output — ground-truth anchor
# ---------------------------------------------------------------------------


def record_llm_output(
    *,
    session_key: str | None,
    usage: dict[str, Any],
    token_budget: int | None = None,
) -> None:
    """Anchor the cache with ground-truth usage from an LLM response.

    The engine's :meth:`LCMEngine.update_from_response` should call this
    on every API response. ``usage`` is the same dict that method
    consumes — this function pulls the same fields and stores the
    composition LCM uses (``input + cacheRead + cacheWrite``).

    Empty ``session_key`` is a no-op (matches TS guard at line 92).

    Args:
        session_key: The Hermes session-key. ``None`` / empty -> no-op.
        usage: The provider's ``usage`` dict. Tolerated shapes:
            * OpenAI Chat: ``prompt_tokens``, ``completion_tokens``,
              ``total_tokens``.
            * Anthropic native: ``input_tokens``, ``output_tokens``,
              ``cache_creation_input_tokens``, ``cache_read_input_tokens``.
            * Hermes-normalized: ``cache_read_tokens`` /
              ``cache_write_tokens`` (ADR-015 patch #4).
            * OpenAI Responses: ``prompt_tokens_details.cached_tokens``.
            The composition is ``input + cache_read + cache_write``
            with each fallback to 0 when absent. ``output`` (the LLM's
            response) is NOT part of the context budget.
        token_budget: Active model's effective context window. ``None``
            keeps the previous value (or stays ``None`` if no prior
            anchor). The gate bypasses when ``None``.
    """
    if not session_key:
        return

    # Composition matches engine.ts:1262-1266: input + cacheRead + cacheWrite.
    # Each fallback to 0 when absent. We accept multiple provider shapes
    # so a single hook can feed any backend.
    input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    cache_read = usage.get("cache_read_tokens") or usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_write_tokens") or usage.get("cache_creation_input_tokens") or 0

    # OpenAI Responses (Codex) nested shape — only the read counter.
    if not cache_read:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cache_read = details.get("cached_tokens") or 0

    current = int(input_tokens) + int(cache_read) + int(cache_write)
    now = time.time()

    with _lock:
        existing = _tokens_by_session.get(session_key)
        resolved_budget = (
            token_budget
            if token_budget is not None
            else (existing.token_budget if existing is not None else None)
        )
        _tokens_by_session[session_key] = TokenSnapshot(
            current_token_count=current,
            token_budget=resolved_budget,
            anchor_at=now,
            last_update_at=now,
            last_update_source="llm_output",
        )


# ---------------------------------------------------------------------------
# accumulate_tool_result_tokens — per-tool additive
# ---------------------------------------------------------------------------


def accumulate_tool_result_tokens(session_key: str | None, result_text: str) -> None:
    """Add a tool result's estimated size to the cache.

    Tools call this in their epilogue (via
    :func:`tap_result_for_token_accounting`) so the next tool call
    within the same iteration sees cumulative state.

    Behaviour:

    * No-op when ``session_key`` is empty.
    * No-op when ``result_text`` is empty.
    * No-op when no anchor exists for ``session_key`` (the first
      ``llm_output`` will set the floor; we don't speculate before then).
    * Otherwise: ``existing.current_token_count += ceil(len(text) / 4)``.

    Args:
        session_key: The Hermes session-key.
        result_text: The tool's result text. Length is divided by 4
            (chars-per-token) to estimate added tokens. ``ceil`` so
            short results still count for >= 1 token.
    """
    if not session_key or not result_text:
        return

    # Match TS: ``Math.ceil(resultText.length / 4)`` — but explicitly use
    # integer arithmetic so we don't depend on floating-point rounding.
    added_tokens = (len(result_text) + 3) // 4
    now = time.time()

    with _lock:
        existing = _tokens_by_session.get(session_key)
        if existing is None:
            # No anchor yet; skip — first llm_output will set the floor.
            # Matches TS guard at line 125.
            return
        _tokens_by_session[session_key] = TokenSnapshot(
            current_token_count=existing.current_token_count + added_tokens,
            token_budget=existing.token_budget,
            anchor_at=existing.anchor_at,
            last_update_at=now,
            last_update_source="tool-self-report",
        )


# ---------------------------------------------------------------------------
# get_runtime_context — accessor for the gate
# ---------------------------------------------------------------------------


def get_runtime_context(session_key: str | None) -> dict[str, Any]:
    """Read the cached snapshot as a dict suitable for the gate.

    The gate (:func:`needs_compact_gate.evaluate_needs_compact_gate`)
    consumes ``current_token_count`` and ``token_budget``; both must be
    present and well-formed for the gate to fire (missing telemetry =
    bypass).

    Args:
        session_key: The Hermes session-key.

    Returns:
        A dict with keys:

        * ``current_token_count`` (int | None) — the cached count.
        * ``token_budget`` (int | None) — the cached budget.
        * ``last_update_at`` (float | None) — write timestamp.
        * ``last_update_source`` (UpdateSource | None) — write source.

        All ``None`` when no anchor exists. Returns an empty dict
        when ``session_key`` is empty (the no-cache contract).
    """
    if not session_key:
        return {}

    with _lock:
        snapshot = _tokens_by_session.get(session_key)
        if snapshot is None:
            return {}
        return {
            "current_token_count": snapshot.current_token_count,
            "token_budget": snapshot.token_budget,
            "last_update_at": snapshot.last_update_at,
            "last_update_source": snapshot.last_update_source,
        }


# ---------------------------------------------------------------------------
# tap_result_for_token_accounting — tool epilogue helper
# ---------------------------------------------------------------------------


def tap_result_for_token_accounting(
    session_key: str | None,
    result_text: str,
) -> str:
    """Accumulate the size of ``result_text`` into the session cache.

    Convenience helper for tool handlers — wraps
    :func:`accumulate_tool_result_tokens` and returns ``result_text``
    unchanged so callers can write::

        return tap_result_for_token_accounting(session_key, tool_result(...))

    Idempotent across re-calls: each call accumulates additively. Tests
    pin the "calling twice sums twice" behavior.

    Args:
        session_key: The Hermes session-key.
        result_text: The tool's emitted result text (typically the
            JSON-encoded string returned by :func:`tool_result`).

    Returns:
        ``result_text`` unchanged (so this function fits in a single
        ``return`` statement).
    """
    accumulate_tool_result_tokens(session_key, result_text)
    return result_text


# ---------------------------------------------------------------------------
# note_successful_compact — Wave-12 W2A1 P0 post-compact cache reset
# ---------------------------------------------------------------------------


def note_successful_compact(session_key: str | None) -> None:
    """Clear the cache entry after a successful ``lcm_compact``.

    Wave-12 audit (W2A1 P0 #1): the engine's actual context drops
    dramatically after compact (e.g. 184K -> 70K). The cache still
    carries the pre-compact value until the next ``llm_output`` fires.
    Without this hook, the very next wrapped tool's
    :func:`needs_compact_gate.evaluate_needs_compact_gate` runs against
    the stale 184K and refuses spuriously — exactly what compact was
    meant to prevent. Worst case: agent loops compact -> refuse ->
    compact until the per-window cap (2 / 5 min) blocks further attempts.

    Strategy: clear the cache entry. The next wrapped tool call sees no
    snapshot -> gate bypasses (its documented behavior when telemetry is
    missing) -> tool runs ->
    :func:`tap_result_for_token_accounting` recreates the entry with the
    size of the new result (small). Subsequent calls accumulate normally;
    on the next ``llm_output`` we snap back to ground truth.

    Conservative: temporarily disables the gate for 1-2 tool calls until
    the next ``llm_output``. That trade is correct here — the alternative
    (stale-cache spurious refusal) is observably worse.

    Args:
        session_key: The Hermes session-key. ``None`` / empty -> no-op.
    """
    if not session_key:
        return
    with _lock:
        _tokens_by_session.pop(session_key, None)


# ---------------------------------------------------------------------------
# Test-only helpers
# ---------------------------------------------------------------------------


def __reset_token_state_for_testing() -> None:
    """Clear the entire cache.

    Use in pytest ``conftest.py`` autouse fixtures or per-test
    ``setUp`` / ``finally`` blocks. Without this, test ordering can leak
    cache entries into unrelated tests.
    """
    with _lock:
        _tokens_by_session.clear()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__: Final = (
    "TokenSnapshot",
    "UpdateSource",
    "accumulate_tool_result_tokens",
    "get_runtime_context",
    "note_successful_compact",
    "record_llm_output",
    "tap_result_for_token_accounting",
)
