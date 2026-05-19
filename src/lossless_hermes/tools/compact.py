"""Port of ``lcm_compact`` — agent-triggered LCM compaction tool.

Ports ``lossless-claw/src/tools/lcm-compact-tool.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 378 LOC TS -> ~340 LOC Python). The
TypeBox-declared schema lives at TS lines 117-126; the factory body /
handler at lines 207-369; the engine-reason mapper at 133-205. All are
translated structurally per ADR-016 (description prose byte-identical
from the TS source).

What this tool does
-------------------

``lcm_compact`` is the **agent-facing wrapper** for
:meth:`LCMEngine.compact`. It lets an agent proactively compact its
conversation context mid-turn when it knows it will need headroom for
subsequent deep-dive tool calls. Closes the gap between "single-call
cap protects one tool" and "post-turn auto-compaction kicks in too
late" — without this middle layer, an agent chaining 4-5 large tool
calls in one turn can hit ``context_length_exceeded`` before the
runtime has a chance to compact.

When the agent should call this (per the description tag):

* Context usage is > 70% AND
* The agent reasonably expects 2+ more tool calls THIS turn AND
* Post-turn compaction will not help (the turn will not end before the
  deep-dive is complete).

8-stage gate sequence
---------------------

Mirrors TS lines 242-369 verbatim:

1. **Operator opt-in** — :attr:`LcmConfig.agent_compaction_tool_enabled`
   must be ``True``. Default is ``False``; the tool is registered
   regardless so the disabled state surfaces to the agent as a structured
   reason rather than "tool not found."
2. **Engine availability** — the :class:`CompactContext` must be wired.
3. **Session key required** — either ``session_key`` or ``session_id``
   must resolve to a non-empty string.
4. **Engine-side gate** — :meth:`CompactContext.get_agent_compaction_gate_state`
   checks ``reserveFraction`` floor, migration health, etc. If
   ``should_refuse`` -> ``{ok: True, compacted: False, reason: gate.refusal_reason}``
   (gate-refusal is "ran successfully and refused").
5. **Per-window cap** — in-memory counter keyed on ``session_key`` with
   max 2 calls per 5-minute window. **LCM Wave-12:** gate-refusals are
   FREE (they do not consume the cap; refused-then-locked-out was a real
   incident — see inline comment at :func:`_check_and_increment_counter`).
   NOT durable across plugin restart (matches TS).
6. **Call :meth:`CompactContext.compact`** — blocking, no timeout.
   Honors engine-side cache-hot + threshold gates.
7. **On success** — :func:`note_successful_compact` clears the token-state
   cache so the next wrapped tool sees fresh ground truth (Wave-12 W2A1
   P0 fix — prevented compact->refuse loops).
8. **Map engine reason** via :func:`_map_engine_reason` -> tool-facing
   enum: ``compacted | noop | auth-failure | session-excluded |
   no-conversation | missing-budget | partial-compact | unknown``.

NOT wrapped in ``run_with_token_gate``
--------------------------------------

Per :data:`needs_compact_gate.TOKEN_GATE_TOOLS`, ``lcm_compact`` is in
the BYPASS set (status response is only ~150 chars; estimator returns
150 tokens). The dispatch layer (issue 06-02) checks this set and
skips the middleware. This is intentional: if the gate refused
``lcm_compact`` calls because context was already > 92%, an agent
trying to recover from near-overflow could not call the very tool
designed to recover.

Architecture seams
------------------

The handler depends on a narrow :class:`CompactContext` Protocol that
exposes:

* ``config: LcmConfig`` — the validated config (for the
  ``agent_compaction_tool_enabled`` flag).
* ``get_agent_compaction_gate_state(...)`` -> :class:`GateState` —
  engine-side gate (reserveFraction floor, migration health).
* ``compact(...)`` -> :class:`CompactionResult` — the actual compaction
  call.

Tests can construct a minimal in-process stand-in without spinning up
the full :class:`LCMEngine`; the eventual 06-02 dispatch wires through
the real engine. Production wiring composes :meth:`LCMEngine.compact`
plus a thin :meth:`LCMEngine.get_agent_compaction_gate_state` adapter
(Epic 04 wrap-up issue).

References
----------

* TS source: ``lossless-claw/src/tools/lcm-compact-tool.ts`` (378 LOC).
* Porting guide: ``docs/porting-guides/tools.md`` §"lcm_compact"
  (lines 494-534).
* Issue spec: ``epics/06-tools/06-14-lcm-compact.md``.
* [ADR-016](../../docs/adr/016-typebox-translation.md) — TypeBox
  hand-translate policy.
* [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-12 rows
  for the per-window cap + post-success cache reset.
* TS test fixture: ``test/v41-lcm-compact-tool.test.ts`` (333 LOC).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Optional, Protocol

from lossless_hermes.compaction import CompactionResult
from lossless_hermes.db.config import LcmConfig
from lossless_hermes.plugin.token_state import note_successful_compact
from lossless_hermes.tools import TOOL_SCHEMAS
from lossless_hermes.tools._common import tool_result
from lossless_hermes.tools._typebox import (
    number_field,
    object_schema,
    optional,
    tool_schema,
)

__all__ = (
    "LCM_COMPACT_DESCRIPTION",
    "LCM_COMPACT_SCHEMA",
    "CompactContext",
    "GateState",
    "RuntimeContext",
    "__reset_lcm_compact_counters_for_testing",
    "handle_lcm_compact",
)


# ===========================================================================
# Schema — verbatim from TS source (ADR-016 §Consequences)
# ===========================================================================
#
# Description prose is byte-identical to lcm-compact-tool.ts lines
# 233-240 (the tool's `description:` block). reserveFraction prose is
# byte-identical to lines 120-123.

LCM_COMPACT_DESCRIPTION: Final[str] = (
    "PROACTIVELY compact this conversation's LCM context mid-turn to free room for chained tool calls. "
    "Use sparingly: only when (a) context is already past 70% of budget AND (b) you reasonably expect 2+ more tool calls this turn AND (c) waiting for post-turn auto-compaction is not viable. "
    "DOES blocking work — typical 5-30s, runs an LLM summarization call. "
    "REFUSES if: context is below the reserveFraction floor (default 50% — no point compacting when context is roomy), engine migration failed at boot, or you've exceeded 2 calls in the last 5 minutes. "
    "DOES NOT gate on prompt-cache state — agent-triggered compaction deliberately bypasses cache deferral that the automatic threshold path uses, because the cache is hot precisely when you most need to compact. "
    "After successful compaction, the next model call will see the compacted view automatically (LCM owns context-engine reassembly between tool calls). "
    "Returns structured reason on success/failure."
)
"""Verbatim from ``lcm-compact-tool.ts:233-240``. Per ADR-016 §Consequences
this is the load-bearing model-facing prose that drives tool selection
(operator-tuning-critical, do not paraphrase)."""


LCM_COMPACT_SCHEMA: Final[dict[str, Any]] = tool_schema(
    name="lcm_compact",
    description=LCM_COMPACT_DESCRIPTION,
    parameters=object_schema(
        reserveFraction=optional(
            number_field(
                "Lower bound on (currentTokens / tokenBudget) before compaction is allowed. Range [0.5, 1.0]. Default 0.5: tool refuses to compact if context is already below half-full (no work needed). Tighter values (e.g. 0.7) make the tool only fire on near-full contexts.",
                minimum=0.5,
                maximum=1.0,
            ),
        ),
    ),
)
"""OpenAI-function-call schema for ``lcm_compact``. Verbatim translation
of the TypeBox declaration at ``lcm-compact-tool.ts:117-126`` per
ADR-016."""


# Register at module import time per the TOOL_SCHEMAS contract documented
# in tools/__init__.py. The 06-02 dispatch table reads via
# ``get_tool_schemas()`` so this side-effect is what makes the tool
# discoverable to the LCMEngine.
TOOL_SCHEMAS.append(LCM_COMPACT_SCHEMA)


# ===========================================================================
# Constants — per-window cap (matches TS lines 74-75)
# ===========================================================================

_COMPACTION_WINDOW_MS: Final[int] = 5 * 60 * 1000
"""Per-window cap timer — 5 minutes — proxy for "turn" until openclaw
plumbs ``turnId`` through to tool execute. Matches TS line 74."""

_DEFAULT_CAP_PER_WINDOW: Final[int] = 2
"""Max ``lcm_compact`` calls per session-key per window. Matches TS line 75."""

_RESERVE_FRACTION_FLOOR: Final[float] = 0.5
"""Hard floor on ``reserveFraction``. Values below clamp up to this.
Matches TS line 282 (``Math.max(0.5, ...)``)."""

_RESERVE_FRACTION_CEILING: Final[float] = 1.0
"""Hard ceiling on ``reserveFraction``. Values above clamp down.
Matches TS line 282 (``Math.min(1.0, r)``)."""


# ===========================================================================
# Per-session counter — in-memory, NOT durable
# ===========================================================================
#
# Mirrors TS ``compactionCallsBySession`` (lines 87-90). Resets when:
#   - Window (5 min) expires since first call
#   - Plugin process restarts
#
# Documented limitation: per-turn cap is advisory anti-abuse, not
# security. Future PR (post-turnId plumbing) switches to runId-keyed.
#
# Python addition (issue 06-14 §Confidence): wrap in ``threading.Lock``
# because the Python port may face concurrent dispatch from asyncio or
# threads (TS was single-threaded). Without the lock, two parallel
# tool-call dispatches could both read ``count=1`` and increment to 2,
# leaving the dict at 2 but having let 3 calls through.


@dataclass
class _CounterEntry:
    """One ``session_key`` -> ``(count, first_at_ms)`` record."""

    count: int
    first_at_ms: int


_counter_lock = threading.Lock()
_compaction_calls_by_session: dict[str, _CounterEntry] = {}


@dataclass(frozen=True)
class _CounterDecision:
    """The result of :func:`_check_and_increment_counter`.

    Attributes:
        allowed: ``True`` when the call should proceed; ``False`` when
            the cap is hit.
        count: The post-increment count (or current count when
            ``allowed=False``).
        reset_at_ms: Wall-clock epoch-ms when this window expires.
    """

    allowed: bool
    count: int
    reset_at_ms: int


def _now_ms() -> int:
    """Return wall-clock epoch milliseconds.

    Factored so tests can monkey-patch ``time.time`` and observe
    deterministic window rollover.
    """
    return int(time.time() * 1000)


def _check_and_increment_counter(
    session_key: str,
    cap_per_window: int = _DEFAULT_CAP_PER_WINDOW,
) -> _CounterDecision:
    """Atomic check-and-increment of the per-window cap.

    Mirrors TS ``checkAndIncrementCounter`` (lines 92-115). The fresh
    entry path resets the window; the existing-entry path increments
    only when below the cap.

    LCM Wave-12: this function MUST run AFTER the engine-side gate so
    gate-refusals don't burn the cap. See the inline comment at the
    call site in :func:`handle_lcm_compact`.

    Args:
        session_key: The session-key keying the counter.
        cap_per_window: The max calls per window. Default
            :data:`_DEFAULT_CAP_PER_WINDOW` (2).

    Returns:
        A :class:`_CounterDecision` with ``allowed`` flag, ``count``,
        and window reset time.
    """
    now = _now_ms()
    with _counter_lock:
        existing = _compaction_calls_by_session.get(session_key)
        if existing is None or now - existing.first_at_ms > _COMPACTION_WINDOW_MS:
            _compaction_calls_by_session[session_key] = _CounterEntry(
                count=1,
                first_at_ms=now,
            )
            return _CounterDecision(
                allowed=True,
                count=1,
                reset_at_ms=now + _COMPACTION_WINDOW_MS,
            )
        if existing.count >= cap_per_window:
            return _CounterDecision(
                allowed=False,
                count=existing.count,
                reset_at_ms=existing.first_at_ms + _COMPACTION_WINDOW_MS,
            )
        existing.count += 1
        return _CounterDecision(
            allowed=True,
            count=existing.count,
            reset_at_ms=existing.first_at_ms + _COMPACTION_WINDOW_MS,
        )


def __reset_lcm_compact_counters_for_testing() -> None:
    """Clear the per-session counter map.

    Test-only helper — name-mangled (double-underscore prefix) so it's
    not part of the public surface. Mirrors TS ``__resetLcmCompactCountersForTesting``
    (lines 376-378). Use in pytest autouse fixtures or per-test
    teardown.
    """
    with _counter_lock:
        _compaction_calls_by_session.clear()


# ===========================================================================
# Protocols — engine seams (CompactContext, RuntimeContext, GateState)
# ===========================================================================


@dataclass(frozen=True)
class GateState:
    """Result of :meth:`CompactContext.get_agent_compaction_gate_state`.

    Mirrors TS ``getAgentCompactionGateState`` return shape (see TS
    test fixture ``v41-tool-harness.ts:114-121``). Producers populate
    ``refusal_reason`` + ``refusal_note`` when ``should_refuse=True``;
    they may leave both ``None`` on the accept path.

    Attributes:
        owns_compaction: Whether the engine actually drives its own
            compaction (vs. degraded after migration failure). Matches
            TS ``ownsCompaction``.
        below_floor: ``True`` when ``contextRatio < reserveFraction``.
            Informational; readers consult ``should_refuse`` for the
            decision.
        should_refuse: The verdict — when ``True``, the tool returns
            the structured refusal payload immediately.
        refusal_reason: Stable tool-facing enum:
            ``"below-floor" | "engine-unhealthy"``. ``None`` when not
            refusing.
        refusal_note: Human-readable explanation suitable for the
            ``note`` field of the tool result. ``None`` when not
            refusing.
        context_ratio: ``currentTokens / tokenBudget`` (rounded to 3
            decimals for stable test expectations). ``None`` when
            telemetry is missing.
    """

    owns_compaction: bool
    below_floor: bool
    should_refuse: bool
    refusal_reason: Optional[str] = None
    refusal_note: Optional[str] = None
    context_ratio: Optional[float] = None


@dataclass(frozen=True)
class RuntimeContext:
    """Live runtime metrics. Mirrors TS ``getRuntimeContext`` return shape.

    Wired to the cached current-token-count + budget populated by the
    ``llm_output`` hook handler (see :mod:`lossless_hermes.plugin.token_state`).
    Returns ``None`` fields when no LLM call has fired yet for this
    session. The tool tolerates ``None`` and skips token-aware logic
    (floor check) in that case — equivalent to "operator hasn't wired
    runtime telemetry yet."

    Attributes:
        current_token_count: Live observed token count. ``None`` when
            no ``llm_output`` has fired.
        token_budget: Effective context budget. ``None`` when not yet
            inferred.
        session_file: Passthrough session-file path used by
            :meth:`CompactContext.compact` (deprecated; use the
            engine's own resolver). Optional.
    """

    current_token_count: Optional[int] = None
    token_budget: Optional[int] = None
    session_file: Optional[str] = None


class CompactContext(Protocol):
    """The handler's collaborator surface.

    Mirrors the slice of :class:`~lossless_hermes.engine.LCMEngine` that
    ``lcm_compact`` actually needs. Using a structural Protocol keeps
    the handler decoupled from the engine class shape and lets tests
    construct a tiny stand-in dataclass.

    Required attributes:

    * ``config``: :class:`LcmConfig` — for the
      ``agent_compaction_tool_enabled`` flag.

    Required methods:

    * ``get_agent_compaction_gate_state(...)`` -> :class:`GateState` —
      engine-side gate; checks ``reserveFraction`` floor + migration
      health. The TS test fixture's default impl mirrors the floor
      logic exactly — production wiring (Epic 04 wrap-up) adds the
      migration / cache-hot probes.
    * ``compact(...)`` -> :class:`CompactionResult` — the actual
      compaction call. Production callers wire this to
      :meth:`LCMEngine.compact`.
    """

    config: LcmConfig

    def get_agent_compaction_gate_state(
        self,
        *,
        session_id: str,
        session_key: str,
        current_token_count: Optional[int],
        token_budget: Optional[int],
        reserve_fraction: float,
    ) -> GateState:
        """Return the gate decision for this call. See :class:`GateState`."""
        ...

    def compact(
        self,
        *,
        session_id: str,
        session_key: str,
        session_file: str,
        token_budget: Optional[int],
        current_token_count: Optional[int],
        force: bool,
    ) -> CompactionResult:
        """Run the actual compaction. See :class:`CompactionResult`."""
        ...


# ===========================================================================
# Engine-reason mapper (TS lines 133-205)
# ===========================================================================


@dataclass(frozen=True)
class _MappedReason:
    """Output of :func:`_map_engine_reason` — tool-facing enum + agent note."""

    tool_reason: str
    agent_note: str


# Tool-facing reason enum — match TS literal union at lines 137-144.
_TOOL_REASON_COMPACTED: Final[str] = "compacted"
_TOOL_REASON_NOOP: Final[str] = "noop"
_TOOL_REASON_AUTH_FAILURE: Final[str] = "auth-failure"
_TOOL_REASON_SESSION_EXCLUDED: Final[str] = "session-excluded"
_TOOL_REASON_NO_CONVERSATION: Final[str] = "no-conversation"
_TOOL_REASON_MISSING_BUDGET: Final[str] = "missing-budget"
_TOOL_REASON_PARTIAL_COMPACT: Final[str] = "partial-compact"
_TOOL_REASON_UNKNOWN: Final[str] = "unknown"


def _map_engine_reason(raw_reason: Optional[str]) -> _MappedReason:
    """Map engine ``CompactionResult.reason`` to the tool-facing enum.

    Mirrors TS ``mapEngineReason`` (lines 133-205). Engine has 12+
    reason strings; the agent does not need that fidelity — collapse
    to a small actionable set.

    Args:
        raw_reason: The engine's raw reason string (``CompactionResult.reason``).
            ``None`` is treated as empty string.

    Returns:
        A :class:`_MappedReason` with the tool-facing enum value and a
        human-readable note suitable for the ``note`` field of the
        tool result.
    """
    r = (raw_reason or "").lower()

    # Order matters: more specific matches before generic substring
    # checks. TS source uses the same branching order.
    if r == "compacted" or "compaction successful" in r:
        return _MappedReason(
            tool_reason=_TOOL_REASON_COMPACTED,
            agent_note="Compaction completed. Next model call sees the compacted view.",
        )

    if (
        "below threshold" in r
        or "already under target" in r
        or "nothing to compact" in r
        or "already compacted" in r
    ):
        return _MappedReason(
            tool_reason=_TOOL_REASON_NOOP,
            agent_note=(
                "No compaction was needed — context is already below threshold or has "
                "nothing compactable. Continue with your work."
            ),
        )

    if "circuit breaker" in r or "auth failure" in r:
        return _MappedReason(
            tool_reason=_TOOL_REASON_AUTH_FAILURE,
            agent_note=(
                "Compaction failed because the summarizer model has lost auth (circuit "
                "breaker tripped). Surface this to the user — operator must "
                "re-authenticate the summarizer provider."
            ),
        )

    if "session excluded" in r or "stateless session" in r:
        return _MappedReason(
            tool_reason=_TOOL_REASON_SESSION_EXCLUDED,
            agent_note=(
                "This session is excluded from LCM (operator config: "
                "ignoreSessionPatterns / statelessSessionPatterns). LCM compaction "
                "does not apply here."
            ),
        )

    if "no conversation found" in r:
        return _MappedReason(
            tool_reason=_TOOL_REASON_NO_CONVERSATION,
            agent_note=(
                "No LCM conversation has been recorded for this session yet — nothing "
                "to compact. (This typically means it's a fresh session with very "
                "little history.)"
            ),
        )

    if "missing token budget" in r:
        return _MappedReason(
            tool_reason=_TOOL_REASON_MISSING_BUDGET,
            agent_note=(
                "Compaction needs the host runtime to provide tokenBudget but it "
                "wasn't available. Pass currentTokenCount + tokenBudget if calling "
                "from automation."
            ),
        )

    if "live context still exceeds target" in r or "deferred compaction no longer needed" in r:
        return _MappedReason(
            tool_reason=_TOOL_REASON_PARTIAL_COMPACT,
            agent_note=(
                "Compaction ran partially — some content was condensed but the "
                "context still exceeds the target. May need another call once the "
                "cache cools, or rely on post-turn compaction."
            ),
        )

    return _MappedReason(
        tool_reason=_TOOL_REASON_UNKNOWN,
        agent_note=(
            f'Compaction returned an unmapped reason: "{raw_reason}". Continue with '
            "your work; check the gateway log if this repeats."
        ),
    )


# ===========================================================================
# Helpers — reserveFraction parsing, ISO timestamp
# ===========================================================================


def _resolve_reserve_fraction(params: dict[str, Any]) -> float:
    """Read ``reserveFraction`` from params with clamping.

    Mirrors TS lines 278-283. Default ``0.5`` when absent / non-finite.
    Clamped to ``[0.5, 1.0]`` — values below the floor are pulled up,
    values above the ceiling are pulled down. Bool is rejected (an
    int subclass; agent providers emit it deliberately).
    """
    raw = params.get("reserveFraction")
    if raw is None or isinstance(raw, bool):
        return _RESERVE_FRACTION_FLOOR
    if not isinstance(raw, (int, float)):
        return _RESERVE_FRACTION_FLOOR
    # Reject NaN / +-inf
    if isinstance(raw, float) and (raw != raw or raw in (float("inf"), float("-inf"))):
        return _RESERVE_FRACTION_FLOOR
    value = float(raw)
    if value < _RESERVE_FRACTION_FLOOR:
        return _RESERVE_FRACTION_FLOOR
    if value > _RESERVE_FRACTION_CEILING:
        return _RESERVE_FRACTION_CEILING
    return value


def _iso_from_ms(ms: int) -> str:
    """Render an epoch-ms timestamp as an ISO-8601 UTC string.

    Matches TS ``new Date(ms).toISOString()`` (e.g.
    ``"2026-05-14T12:34:56.789Z"``).
    """
    return (
        datetime
        .fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ===========================================================================
# Handler entry point
# ===========================================================================


def handle_lcm_compact(
    args: dict[str, Any],
    *,
    ctx: Optional[CompactContext],
    session_key: Optional[str] = None,
    session_id: Optional[str] = None,
    runtime_context: Optional[RuntimeContext] = None,
) -> str:
    """Handle an ``lcm_compact`` tool call.

    This is the FULL handler — ``lcm_compact`` is in the
    :data:`needs_compact_gate.TOKEN_GATE_TOOLS` BYPASS set, so the
    dispatch layer (issue 06-02) does NOT wrap this call in
    :func:`run_with_token_gate`. Status response is only ~150 chars;
    gating it would create a paradox (the tool designed to recover
    from near-overflow gets refused at near-overflow).

    Args:
        args: The tool-call ``arguments`` dict from the LLM provider.
            Read defensively — see :mod:`lossless_hermes.tools._common`.
        ctx: A :class:`CompactContext` exposing the engine seams. When
            ``None``, the tool returns ``engine-unavailable`` — matches
            the TS pattern (plugin still booting).
        session_key: Optional cross-conversation session-family key.
            Used as the cap counter key and forwarded to the engine.
        session_id: Optional runtime session id. Used as fallback when
            ``session_key`` is empty.
        runtime_context: Optional :class:`RuntimeContext` carrying
            ``current_token_count`` + ``token_budget`` + ``session_file``.
            When ``None`` an empty :class:`RuntimeContext` is used.

    Returns:
        A JSON string per the :func:`tool_result` contract — the inner
        payload is one of:

        * ``{ok: False, compacted: False, reason: "operator-disabled", note: ...}``
        * ``{ok: False, compacted: False, reason: "engine-unavailable", note: ...}``
        * ``{ok: False, compacted: False, reason: "no-session", note: ...}``
        * ``{ok: True,  compacted: False, reason: "below-floor", note: ..., contextRatio: ...}``
        * ``{ok: True,  compacted: False, reason: "engine-unhealthy", note: ...}``
        * ``{ok: False, compacted: False, reason: "capped-this-turn", note: ..., retryAfterIso: ...}``
        * ``{ok: ..., compacted: ..., reason: <mapped enum>, note: ..., rawEngineReason: ..., contextRatio: ..., callsThisWindow: ..., callsRemainingThisWindow: ...}``
        * ``{ok: False, compacted: False, reason: "exception", note: ...}``
    """
    # ----- Stage 1: Operator opt-in gate (TS lines 247-256) ----------------
    # Always-register pattern: the tool surfaces the disabled state to
    # the agent rather than returning "tool not found", so the agent can
    # recommend operator action.
    if ctx is None or not getattr(ctx.config, "agent_compaction_tool_enabled", False):
        if ctx is None:
            # Special-case: no ctx means we cannot even inspect the
            # config. Treat as engine-unavailable, not operator-disabled.
            return tool_result(
                {
                    "ok": False,
                    "compacted": False,
                    "reason": "engine-unavailable",
                    "note": (
                        "LCM engine is not available. The plugin may still be "
                        "initializing — try again on the next turn."
                    ),
                },
            )
        return tool_result(
            {
                "ok": False,
                "compacted": False,
                "reason": "operator-disabled",
                "note": (
                    "lcm_compact is disabled by operator config. To enable, set "
                    "agentCompactionToolEnabled: true in the lossless-claw plugin "
                    "config and restart the gateway."
                ),
            },
        )

    # ----- Stage 2: Engine availability (TS lines 258-266) -----------------
    # ctx is non-None at this point (Stage 1 handled the None case).

    # ----- Stage 3: Session key resolution (TS lines 268-276) --------------
    # session_key wins over session_id per TS line 268 (??). Both are
    # ``str.strip()``'d to drop whitespace-only inputs.
    effective_key = ""
    if isinstance(session_key, str) and session_key.strip():
        effective_key = session_key.strip()
    elif isinstance(session_id, str) and session_id.strip():
        effective_key = session_id.strip()
    if not effective_key:
        return tool_result(
            {
                "ok": False,
                "compacted": False,
                "reason": "no-session",
                "note": ("No session-key was provided to the tool factory; cannot compact."),
            },
        )

    # ----- Resolve reserveFraction + runtime context -----------------------
    reserve_fraction = _resolve_reserve_fraction(args)
    rt = runtime_context if runtime_context is not None else RuntimeContext()

    # Resolve the effective session_id passed to the engine — TS uses
    # ``input.sessionId ?? sessionKey`` at line 299/332.
    effective_session_id = (
        session_id.strip() if isinstance(session_id, str) and session_id.strip() else effective_key
    )

    # ----- Stage 4: Engine-side gate (TS lines 291-313) --------------------
    # LCM Wave-12: gate-refusals don't burn the per-window cap.
    # If we counted refusals, an agent that ran into the floor would be locked out
    # for 5 minutes even when the floor was the right answer.
    # Original: lossless-claw/src/tools/lcm-compact-tool.ts:291-298.
    gate = ctx.get_agent_compaction_gate_state(
        session_id=effective_session_id,
        session_key=effective_key,
        current_token_count=rt.current_token_count,
        token_budget=rt.token_budget,
        reserve_fraction=reserve_fraction,
    )
    if gate.should_refuse:
        # Gate-refusal is "tool ran successfully and refused" — ok=True
        # so the agent can distinguish this from outright errors. Matches
        # TS line 307 (``ok: true``).
        return tool_result(
            {
                "ok": True,
                "compacted": False,
                "reason": gate.refusal_reason or "engine-unhealthy",
                "note": gate.refusal_note or "",
                "contextRatio": gate.context_ratio,
            },
        )

    # ----- Stage 5: Per-window cap (TS lines 315-326) ----------------------
    # Increment now that the gate has accepted — this counts as a real
    # compaction attempt.
    cap = _check_and_increment_counter(effective_key, _DEFAULT_CAP_PER_WINDOW)
    if not cap.allowed:
        window_min = round(_COMPACTION_WINDOW_MS / 60_000)
        return tool_result(
            {
                "ok": False,
                "compacted": False,
                "reason": "capped-this-turn",
                "note": (
                    f"Per-window compaction cap reached ({cap.count}/"
                    f"{_DEFAULT_CAP_PER_WINDOW} in the last {window_min} min). "
                    f"Counter resets at {_iso_from_ms(cap.reset_at_ms)}. "
                    "Continue with your existing context — chained calls will queue "
                    "post-turn compaction automatically."
                ),
                "retryAfterIso": _iso_from_ms(cap.reset_at_ms),
            },
        )

    # ----- Stage 6: Run the actual compaction (TS lines 328-358) -----------
    # Blocking, no timeout — see module docstring "Limitations" section.
    session_file = rt.session_file or ""
    try:
        result = ctx.compact(
            session_id=effective_session_id,
            session_key=effective_key,
            session_file=session_file,
            token_budget=rt.token_budget,
            current_token_count=rt.current_token_count,
            force=False,  # honors engine-side cache-hot + threshold gates
        )
    except Exception as exc:  # noqa: BLE001 — TS catches the equivalent broadly
        # Engine throws -> {ok: False, reason: "exception"}. Don't propagate
        # to the caller. Matches TS catch block at lines 359-365.
        return tool_result(
            {
                "ok": False,
                "compacted": False,
                "reason": "exception",
                "note": (
                    f"Compaction threw: {exc}. Check the gateway log; surface to the "
                    "user if this repeats."
                ),
            },
        )

    # ----- Stage 7: Post-success cache reset (TS lines 339-347) ------------
    # LCM Wave-12 W2A1 P0: clear the token-state cache on successful compact
    # so the next wrapped tool's gate computes fresh ground truth.
    # Without this clear, a compact->refuse loop forms (cached high ratio
    # survives the compact, the next gated tool refuses, and so on).
    # Original: lossless-claw/src/tools/lcm-compact-tool.ts:339-347.
    if _result_indicates_compaction(result):
        note_successful_compact(effective_key)

    # ----- Stage 8: Map engine reason + emit (TS lines 348-358) ------------
    mapped = _map_engine_reason(result.reason)
    return tool_result(
        {
            "ok": _result_ok(result),
            "compacted": _result_indicates_compaction(result),
            "reason": mapped.tool_reason,
            "note": mapped.agent_note,
            "rawEngineReason": result.reason,
            "contextRatio": gate.context_ratio,
            "callsThisWindow": cap.count,
            "callsRemainingThisWindow": max(0, _DEFAULT_CAP_PER_WINDOW - cap.count),
        },
    )


# ===========================================================================
# CompactionResult adapters
# ===========================================================================


#: ``CompactionResult.reason`` substrings that are genuine *failures* —
#: i.e. the TS ``CompactResult`` envelope returns ``ok: false`` for them
#: even though they are not auth failures. The match is substring + lower
#: cased, identical to the keying :func:`_map_engine_reason` uses.
#:
#: Currently this is the single ``"missing token budget"`` reason. TS
#: ``executeCompactionCore`` (``engine.ts:3363-3369``) returns
#: ``{ok: false, compacted: false, reason: "missing token budget in
#: compact params"}`` — a *budget* problem, not an auth problem. The
#: Python ``LCMEngine.compact()`` takes a non-``Optional`` ``int`` budget
#: so it can never itself emit this reason; the
#: :class:`~lossless_hermes.tools._adapters._CompactCtx` shim's
#: missing-budget guard is its sole producer. Listing the reason here
#: (rather than flipping ``auth_failure``) keeps the failure *honest*:
#: ``_map_engine_reason`` still maps it to the ``missing-budget`` tool
#: enum and the agent-facing note still says "needs the host runtime to
#: provide tokenBudget" — it is not mislabelled as an auth failure.
_NON_AUTH_FAILURE_REASON_SUBSTRINGS: Final[tuple[str, ...]] = ("missing token budget",)


def _result_ok(result: CompactionResult) -> bool:
    """Return the TS-equivalent ``ok`` flag for a :class:`CompactionResult`.

    The TS ``CompactResult`` has an explicit ``ok`` field; the Python
    :class:`CompactionResult` represents the same condition via
    ``auth_failure`` + ``reason``. Mapping:

    * ``auth_failure=True`` -> ``ok=False`` (circuit breaker / provider
      auth tripped).
    * ``reason`` matching a :data:`_NON_AUTH_FAILURE_REASON_SUBSTRINGS`
      entry -> ``ok=False``. These are genuine failures that are NOT
      auth failures — TS returns ``ok: false`` for them too. Currently
      just ``"missing token budget"``: TS ``executeCompactionCore``
      (``engine.ts:3363-3369``) returns ``ok: false`` for the
      missing-budget no-op, so a :class:`CompactionResult` carrying that
      ``reason`` must report ``ok=False`` — without being mislabelled
      ``auth_failure`` (it is a budget problem, not an auth problem).
    * Otherwise -> ``ok=True``. Note the ``"no conversation found"``
      no-op deliberately stays ``ok=True`` (TS ``engine.ts:7223-7227``
      likewise returns ``ok: true`` for it) — only genuine failures
      flip the bit.
    """
    if result.auth_failure:
        return False
    reason = (result.reason or "").lower()
    if any(sub in reason for sub in _NON_AUTH_FAILURE_REASON_SUBSTRINGS):
        return False
    return True


def _result_indicates_compaction(result: CompactionResult) -> bool:
    """Return the TS-equivalent ``compacted`` flag.

    Mirrors TS line 351 (``Boolean(result.compacted)``). The Python
    :class:`CompactionResult` represents the same condition via
    ``action_taken``, which is ``True`` iff at least one pass ran to
    completion.
    """
    return bool(result.action_taken)
