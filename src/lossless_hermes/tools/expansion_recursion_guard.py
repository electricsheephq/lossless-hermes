"""Guard against infinite recursion in delegated ``lcm_expand`` chains.

Ports ``lossless-claw/src/tools/lcm-expansion-recursion-guard.ts`` (LCM
commit ``1f07fbd`` on branch ``pr-613``, 373 LOC TS → ~430 LOC Python).

### What this module enforces

When a session calls ``lcm_expand_query`` it can spawn a delegated
sub-agent session that, by design, runs with reduced privileges. The
two invariants this module enforces:

1. **Recursion depth cap** — a delegated session is allowed exactly one
   level of expansion delegation. A delegated child re-entering
   ``lcm_expand_query`` is blocked with ``depth_cap`` (or
   ``idempotent_reentry`` if the same ``request_id`` repeats — distinct
   reason so the caller can surface a clearer error message).
2. **Per-origin concurrency** — only one delegated expansion may be
   in flight per origin session at a time. A second concurrent caller
   from the same origin is blocked with ``origin_session_in_flight``
   rather than racing for shared resources.

Both invariants are tracked in process-local in-memory state because
delegated expansion is always within a single Python process; cross-
process delegation does not exist in the LCM v4.1 architecture (per
``docs/porting-guides/tools.md`` §lcm-expansion-recursion-guard).

### State maps

Three module-level dicts protected by a single :class:`threading.Lock`
(per ADR-017 — the LCM SQL surface is sync; concurrent callers reach
this guard via Hermes's thread-per-request executor):

* ``_delegated_context_by_session_key`` — child session_key →
  :class:`DelegatedExpansionContext` (the metadata stamped when the
  parent delegates).
* ``_blocked_request_ids_by_session_key`` — child session_key → set of
  request_ids that have already been blocked. Used to distinguish
  first-block (``depth_cap``) from re-entry (``idempotent_reentry``).
* ``_active_request_id_by_origin_session_key`` — origin session_key →
  the request_id currently holding that origin's concurrency slot.

### Async-vs-sync choice

The TS source uses module-level closures and a single-threaded event
loop; concurrency safety is implicit. Python runs LCM tools off the
Hermes thread-pool executor (sync SQL surface, per ADR-017), so a
shared :class:`threading.Lock` is the minimum-viable concurrency
primitive. The lock is held only for the duration of map mutations —
the rest of the function bodies (string concat, dict reads) are
intentionally outside the critical section to keep contention narrow.

### Telemetry

Monotonic counters live in module-level
``_telemetry_counters: dict[TelemetryEvent, int]``. They are reset
only via :func:`reset_for_tests`. Each
:func:`record_expansion_delegation_telemetry` call increments the
event's counter and emits a structured log line through
``logging.getLogger("lcm.expansion")``.

### Fallback delegated-grant context

The TS source falls back to :func:`resolveDelegatedExpansionGrantId`
(from ``src/expansion-auth.ts``) when no stamped context exists. The
Python port hooks this up via an injectable callable
:data:`_grant_resolver`: tests/callers can override it with a
function that returns a grant_id string when a session has been
issued a delegated grant. By default the resolver returns ``None``
(no grant) — the ``expansion_auth`` Python module has not yet been
ported (issue 06-XX in epic 06; outside this issue's scope).

See:

* TS source: ``lossless-claw/src/tools/lcm-expansion-recursion-guard.ts``
* Porting guide: ``docs/porting-guides/tools.md`` §lcm-expansion-
  recursion-guard.ts (lines 561-580).
* ADR-012 — ``lcm_expand_query`` deferred to v2 but this guard still
  ports because ``lcm_expand`` queries the depth state via
  :func:`resolve_next_expansion_depth` even when invoked directly.
* ADR-017 — synchronous-by-design.
* ADR-029 — Wave-N provenance (no Wave-N markers in this module).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Final, Literal, Optional

__all__ = [
    "DelegatedExpansionContext",
    "EXPANSION_CONCURRENCY_ERROR_CODE",
    "EXPANSION_DELEGATION_DEPTH_CAP",
    "EXPANSION_RECURSION_ERROR_CODE",
    "ExpansionConcurrencyBlockReason",
    "ExpansionConcurrencyGuardDecision",
    "ExpansionRecursionBlockReason",
    "ExpansionRecursionGuardDecision",
    "StampInput",
    "TelemetryEvent",
    "acquire_expansion_concurrency_slot",
    "clear_delegated_expansion_context",
    "create_expansion_request_id",
    "evaluate_expansion_recursion_guard",
    "get_delegated_expansion_context_for_tests",
    "get_expansion_delegation_telemetry_snapshot_for_tests",
    "record_expansion_delegation_telemetry",
    "release_expansion_concurrency_slot",
    "reset_for_tests",
    "resolve_expansion_request_id",
    "resolve_next_expansion_depth",
    "set_delegated_grant_resolver",
    "stamp_delegated_expansion_context",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

EXPANSION_RECURSION_ERROR_CODE: Final[str] = "EXPANSION_RECURSION_BLOCKED"
"""Error code surfaced when a delegated session exceeds the recursion cap."""

EXPANSION_CONCURRENCY_ERROR_CODE: Final[str] = "EXPANSION_CONCURRENCY_BLOCKED"
"""Error code surfaced when an origin session has an active expansion slot."""

EXPANSION_DELEGATION_DEPTH_CAP: Final[int] = 1
"""Hard cap on delegated expansion depth.

A delegated child session is permitted depth ``<= EXPANSION_DELEGATION_DEPTH_CAP``.
A grandchild (depth 2) is blocked. The constant is intentionally kept as
``1`` to match TS — bumping it requires an ADR and matching changes in
the auth grant layer.
"""

_LOG = logging.getLogger("lcm.expansion")


# ---------------------------------------------------------------------------
# Public types (dataclasses + literal unions)
# ---------------------------------------------------------------------------


TelemetryEvent = Literal["start", "block", "timeout", "success"]
"""Discrete telemetry event the guard emits."""

ExpansionRecursionBlockReason = Literal["depth_cap", "idempotent_reentry"]
"""Why a recursion-guard block was issued."""

ExpansionConcurrencyBlockReason = Literal["origin_session_in_flight"]
"""Why a concurrency-guard block was issued."""


@dataclass(frozen=True)
class DelegatedExpansionContext:
    """Metadata stamped on a delegated child session.

    Attributes:
        request_id: Stable request identifier shared across the
            delegated expansion tree.
        expansion_depth: 1-based depth of this delegation level. Always
            clamped to ``>= 0`` and integer-truncated on stamp.
        origin_session_key: The session_key of the *origin* (top-level)
            session that issued the delegation. Used by recovery
            guidance messages so the agent knows where to retry.
        stamped_by: Free-form string identifying the code path that
            stamped this context (e.g. ``"delegated_grant"`` for the
            auth-grant fallback path, or ``"lcm_expand_query"`` for the
            normal delegation entry point).
        created_at: ISO-8601 UTC timestamp of the stamp call.
    """

    request_id: str
    expansion_depth: int
    origin_session_key: str
    stamped_by: str
    created_at: str


@dataclass(frozen=True)
class StampInput:
    """Required keyword inputs for :func:`stamp_delegated_expansion_context`.

    Mirrors the TS object-arg shape so reviewers can map TS ↔ Python
    field-by-field. Public so callers can construct it as a value
    object; nothing forces use over passing kwargs directly.
    """

    session_key: str
    request_id: str
    expansion_depth: int
    origin_session_key: str
    stamped_by: str


@dataclass(frozen=True)
class _RecursionGuardAllowed:
    """Allowed branch of :class:`ExpansionRecursionGuardDecision`."""

    blocked: Literal[False]
    request_id: str
    expansion_depth: int
    origin_session_key: str


@dataclass(frozen=True)
class _RecursionGuardBlocked:
    """Blocked branch of :class:`ExpansionRecursionGuardDecision`."""

    blocked: Literal[True]
    code: str
    reason: ExpansionRecursionBlockReason
    message: str
    request_id: str
    expansion_depth: int
    origin_session_key: str


ExpansionRecursionGuardDecision = _RecursionGuardAllowed | _RecursionGuardBlocked
"""Discriminated union: ``blocked=True`` carries the error envelope.

Mirrors the TS discriminated-union type. Pattern-match on ``blocked``:

* ``decision.blocked is False`` → take the allowed branch, read
  ``request_id`` + ``expansion_depth`` + ``origin_session_key``.
* ``decision.blocked is True`` → return the structured error to the
  caller; ``message`` already includes the recovery guidance.
"""


@dataclass(frozen=True)
class _ConcurrencyGuardAllowed:
    """Allowed branch of :class:`ExpansionConcurrencyGuardDecision`."""

    blocked: Literal[False]
    request_id: str
    origin_session_key: str


@dataclass(frozen=True)
class _ConcurrencyGuardBlocked:
    """Blocked branch of :class:`ExpansionConcurrencyGuardDecision`."""

    blocked: Literal[True]
    code: str
    reason: ExpansionConcurrencyBlockReason
    message: str
    request_id: str
    origin_session_key: str


ExpansionConcurrencyGuardDecision = _ConcurrencyGuardAllowed | _ConcurrencyGuardBlocked


# ---------------------------------------------------------------------------
# Module-level state (lock-protected)
# ---------------------------------------------------------------------------

_state_lock: Final[threading.Lock] = threading.Lock()
"""Single lock protecting all three state maps + the telemetry counters.

Granularity choice: per-map locks would be slightly faster in the
no-contention case but introduce ordering hazards (the recursion guard
mutates the blocked-ids map and reads the delegated-context map in the
same call). A single coarse lock is simpler and correct; contention is
expected to be negligible because the critical sections are tiny dict
operations.
"""

_delegated_context_by_session_key: dict[str, DelegatedExpansionContext] = {}
_blocked_request_ids_by_session_key: dict[str, set[str]] = {}
_active_request_id_by_origin_session_key: dict[str, str] = {}

_telemetry_counters: dict[TelemetryEvent, int] = {
    "start": 0,
    "block": 0,
    "timeout": 0,
    "success": 0,
}


# ---------------------------------------------------------------------------
# Pluggable grant resolver (default: no-op)
# ---------------------------------------------------------------------------

_GrantResolver = Callable[[str], Optional[str]]


def _default_grant_resolver(session_key: str) -> Optional[str]:
    """Default resolver — returns ``None`` (no grant).

    The TS source consults ``resolveDelegatedExpansionGrantId`` from
    ``src/expansion-auth.ts``. The Python ``expansion_auth`` module is
    not yet ported (epic 06; this issue covers the recursion guard
    only). Until then, the default resolver is a no-op so the
    fallback-context branch in :func:`evaluate_expansion_recursion_guard`
    is effectively inert. When ``expansion_auth`` lands, callers can
    register the real resolver via :func:`set_delegated_grant_resolver`.

    Args:
        session_key: The candidate session key.

    Returns:
        Always ``None`` for the default resolver.
    """
    del session_key  # explicit signal that the argument is intentionally unused
    return None


_grant_resolver: _GrantResolver = _default_grant_resolver


def set_delegated_grant_resolver(resolver: Optional[_GrantResolver]) -> None:
    """Register a custom delegated-grant resolver.

    Used by the ``expansion_auth`` module (when it lands) to wire the
    real grant lookup. Tests can pass a stub. Passing ``None`` restores
    the no-op default.

    Thread-safety: a plain assignment is atomic in CPython for module
    attributes; no lock needed.

    Args:
        resolver: Callable mapping a session_key string to a grant_id
            string (or ``None`` when no grant exists). ``None`` resets
            to the no-op default.
    """
    global _grant_resolver
    _grant_resolver = resolver if resolver is not None else _default_grant_resolver


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalize_session_key(session_key: Optional[str]) -> str:
    """Trim whitespace and reject non-string inputs.

    Mirrors TS ``normalizeSessionKey`` (line 65-67 of source). Non-string
    inputs (including ``None``) collapse to the empty string.

    Args:
        session_key: Candidate session key, possibly ``None`` or a
            string with leading/trailing whitespace.

    Returns:
        The trimmed string, or ``""`` for non-string inputs.
    """
    if not isinstance(session_key, str):
        return ""
    return session_key.strip()


def _get_or_init_blocked_request_ids(session_key: str) -> set[str]:
    """Return the blocked-id set for a session_key, creating it if absent.

    MUST be called with :data:`_state_lock` held.
    """
    existing = _blocked_request_ids_by_session_key.get(session_key)
    if existing is not None:
        return existing
    created: set[str] = set()
    _blocked_request_ids_by_session_key[session_key] = created
    return created


def _utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 with ``Z`` suffix.

    Matches TS ``new Date().toISOString()`` (e.g. ``"2026-05-14T03:14:15.926Z"``).
    Python's :meth:`datetime.isoformat` defaults to ``+00:00`` for UTC;
    we manually replace to keep byte-parity with TS log lines.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _resolve_fallback_delegated_context(
    session_key: str,
    request_id: str,
) -> Optional[DelegatedExpansionContext]:
    """Fallback to the expansion-auth grant when no stamped context exists.

    Mirrors TS ``resolveFallbackDelegatedContext`` (lines 79-97). Returns
    a synthetic :class:`DelegatedExpansionContext` pretending the
    grantee was stamped at the depth cap — so a delegated session that
    arrived via the auth-grant path is treated identically to a stamped
    delegation for recursion-guard purposes.

    Args:
        session_key: Already-normalized session key.
        request_id: Request id to record in the synthetic context.

    Returns:
        Synthetic context if the resolver finds a grant, else ``None``.
    """
    if not session_key:
        return None
    grant_id = _grant_resolver(session_key)
    if not grant_id:
        return None
    return DelegatedExpansionContext(
        request_id=request_id,
        expansion_depth=EXPANSION_DELEGATION_DEPTH_CAP,
        origin_session_key=session_key,
        stamped_by="delegated_grant",
        created_at=_utc_now_iso(),
    )


def _build_expansion_recursion_recovery_guidance(origin_session_key: str) -> str:
    """Construct the recursion-block recovery hint shown to the agent."""
    return (
        "Recovery: In delegated sub-agent sessions, call `lcm_expand` directly "
        "and synthesize your answer from that result. Do NOT call "
        "`lcm_expand_query` from delegated context. If deeper delegation is "
        f"required, return to the origin session ({origin_session_key}) and "
        "call `lcm_expand_query` there."
    )


def _build_expansion_concurrency_recovery_guidance(origin_session_key: str) -> str:
    """Construct the concurrency-block recovery hint shown to the agent."""
    return (
        "Recovery: Wait for the active expansion to finish before retrying. "
        f"If you need an immediate fallback, stay in the origin session "
        f"({origin_session_key}) and use `lcm_grep` or `lcm_describe` instead."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_expansion_request_id() -> str:
    """Mint a fresh request id for a delegated expansion call.

    Mirrors TS ``createExpansionRequestId`` (uses ``crypto.randomUUID()``).
    Python uses :func:`uuid.uuid4` and surfaces the canonical
    hyphen-separated form (matching JS ``randomUUID`` exactly).

    Returns:
        A fresh UUID4 string, e.g. ``"f81d4fae-7dec-11d0-a765-00a0c91e6bf6"``.
    """
    return str(uuid.uuid4())


def resolve_expansion_request_id(session_key: Optional[str] = None) -> str:
    """Resolve (or mint) the active expansion request id for a session.

    If a delegated context has been stamped for this session, return
    its ``request_id`` so the entire delegated tree shares one id; else
    mint a fresh one.

    Args:
        session_key: Optional session key to look up. Empty/missing
            input always yields a fresh id.

    Returns:
        The stamped ``request_id`` if present, else a new UUID4 string.
    """
    key = _normalize_session_key(session_key)
    with _state_lock:
        existing = _delegated_context_by_session_key.get(key)
    if existing is not None:
        return existing.request_id
    return create_expansion_request_id()


def resolve_next_expansion_depth(session_key: Optional[str] = None) -> int:
    """Resolve the depth to stamp onto a *child* of this session.

    Mirrors TS ``resolveNextExpansionDepth`` (lines 137-148). Returns:

    * ``1`` for a fresh session with no stamped context and no
      auth-grant binding (this is the normal top-level case).
    * ``stamped.expansion_depth + 1`` when this session already has a
      stamped delegated context (the next child is one level deeper).
    * ``EXPANSION_DELEGATION_DEPTH_CAP + 1`` when this session has an
      auth-grant binding but no stamped context (the grant implies
      depth-cap baseline).

    Args:
        session_key: Optional session key. Empty/missing always
            returns ``1``.

    Returns:
        The depth integer to stamp on a child of this session.
    """
    key = _normalize_session_key(session_key)
    if not key:
        return 1
    with _state_lock:
        existing = _delegated_context_by_session_key.get(key)
    if existing is not None:
        return existing.expansion_depth + 1
    if _grant_resolver(key):
        return EXPANSION_DELEGATION_DEPTH_CAP + 1
    return 1


def stamp_delegated_expansion_context(
    *,
    session_key: str,
    request_id: str,
    expansion_depth: int,
    origin_session_key: str,
    stamped_by: str,
) -> DelegatedExpansionContext:
    """Stamp delegated metadata onto a child session so re-entry is detectable.

    Mirrors TS ``stampDelegatedExpansionContext`` (lines 154-173). The
    ``expansion_depth`` is clamped to ``>= 0`` and integer-truncated
    (TS uses ``Math.trunc``; Python uses :func:`int`-cast on the
    clamped value — same outcome for finite inputs).

    A blank ``origin_session_key`` collapses to ``"main"`` for parity
    with TS (a delegated context with no origin is conceptually a top-
    level call; ``"main"`` is the agreed sentinel).

    The stamp is a no-op when ``session_key`` is empty — there is no
    valid child to attach metadata to. The function still returns the
    constructed context object so the caller can log it, but the
    module state is untouched.

    Args:
        session_key: The child session_key receiving the stamp.
        request_id: Request id to propagate through the delegation tree.
        expansion_depth: Depth to stamp (clamped to ``>= 0``,
            truncated to int).
        origin_session_key: Top-level session key; ``"main"`` if blank.
        stamped_by: Identifier of the stamping code path (free-form).

    Returns:
        The :class:`DelegatedExpansionContext` that was stamped (or
        would have been, if ``session_key`` is blank).
    """
    key = _normalize_session_key(session_key)
    clamped_depth = max(0, int(expansion_depth))
    normalized_origin = origin_session_key.strip() if isinstance(origin_session_key, str) else ""
    context = DelegatedExpansionContext(
        request_id=request_id,
        expansion_depth=clamped_depth,
        origin_session_key=normalized_origin or "main",
        stamped_by=stamped_by,
        created_at=_utc_now_iso(),
    )
    if key:
        with _state_lock:
            _delegated_context_by_session_key[key] = context
    return context


def clear_delegated_expansion_context(session_key: str) -> None:
    """Remove delegated metadata + any blocked-id history for a session.

    Mirrors TS ``clearDelegatedExpansionContext`` (lines 177-185). Used
    by the post-expansion cleanup path. Empty session_key is a no-op.

    Args:
        session_key: Child session key whose metadata to clear.
    """
    key = _normalize_session_key(session_key)
    if not key:
        return
    with _state_lock:
        _delegated_context_by_session_key.pop(key, None)
        _blocked_request_ids_by_session_key.pop(key, None)


def evaluate_expansion_recursion_guard(
    *,
    session_key: Optional[str] = None,
    request_id: str,
) -> ExpansionRecursionGuardDecision:
    """Decide whether a delegated session may delegate one more level.

    Mirrors TS ``evaluateExpansionRecursionGuard`` (lines 192-240). The
    decision tree:

    1. If no delegated context is stamped AND no auth-grant fallback
       applies → allowed (this is a top-level call).
    2. If a context exists at depth ``< EXPANSION_DELEGATION_DEPTH_CAP``
       → allowed (one more level is fine).
    3. Otherwise → blocked. The block reason is
       ``idempotent_reentry`` if this exact ``request_id`` has already
       been blocked for this session, else ``depth_cap`` (the first
       block for this session+request_id).

    Args:
        session_key: The session key initiating the would-be delegation.
        request_id: The request id of the would-be delegation.

    Returns:
        :class:`ExpansionRecursionGuardDecision` — read ``.blocked`` to
        choose the branch.
    """
    key = _normalize_session_key(session_key)
    trimmed_request_id = request_id.strip()
    fallback_seed = trimmed_request_id or create_expansion_request_id()

    with _state_lock:
        delegated_context = _delegated_context_by_session_key.get(key)
        if delegated_context is None:
            # The grant-resolver call must happen outside the critical
            # section if it ever does IO, but the default resolver is
            # a no-op and an injected resolver is the caller's
            # responsibility. We keep the call inside the lock to keep
            # the decision atomic with respect to a concurrent stamp.
            delegated_context = _resolve_fallback_delegated_context(key, fallback_seed)

        if delegated_context is None:
            return _RecursionGuardAllowed(
                blocked=False,
                request_id=trimmed_request_id,
                expansion_depth=0,
                origin_session_key=key or "main",
            )

        if delegated_context.expansion_depth < EXPANSION_DELEGATION_DEPTH_CAP:
            return _RecursionGuardAllowed(
                blocked=False,
                request_id=trimmed_request_id,
                expansion_depth=delegated_context.expansion_depth,
                origin_session_key=delegated_context.origin_session_key,
            )

        seen_request_ids = _get_or_init_blocked_request_ids(key)
        is_idempotent_reentry = trimmed_request_id in seen_request_ids
        seen_request_ids.add(trimmed_request_id)
        reason: ExpansionRecursionBlockReason = (
            "idempotent_reentry" if is_idempotent_reentry else "depth_cap"
        )

    # Build message outside the lock — pure string work, no shared state.
    message = (
        f"{EXPANSION_RECURSION_ERROR_CODE}: Expansion delegation blocked at "
        f"depth {delegated_context.expansion_depth} ({reason}; "
        f"requestId={trimmed_request_id}; "
        f"origin={delegated_context.origin_session_key}). "
    ) + _build_expansion_recursion_recovery_guidance(delegated_context.origin_session_key)

    return _RecursionGuardBlocked(
        blocked=True,
        code=EXPANSION_RECURSION_ERROR_CODE,
        reason=reason,
        message=message,
        request_id=trimmed_request_id,
        expansion_depth=delegated_context.expansion_depth,
        origin_session_key=delegated_context.origin_session_key,
    )


def acquire_expansion_concurrency_slot(
    *,
    origin_session_key: Optional[str] = None,
    request_id: str,
) -> ExpansionConcurrencyGuardDecision:
    """Acquire the single in-flight slot for ``origin_session_key``.

    Mirrors TS ``acquireExpansionConcurrencySlot`` (lines 247-278). The
    behavior:

    * If no slot is held → claim it for ``request_id`` and return allowed.
    * If the slot is held by **this** ``request_id`` → idempotent allowed
      (the caller is re-acquiring after a transient retry). Slot
      ownership is unchanged.
    * If the slot is held by a **different** ``request_id`` → blocked
      with ``origin_session_in_flight``.

    The blank ``origin_session_key`` collapses to ``"main"`` for parity
    with TS (top-level callers are conceptually the ``"main"`` origin).

    Args:
        origin_session_key: Top-level origin session key. Blank →
            ``"main"``.
        request_id: The expansion request id requesting the slot.

    Returns:
        :class:`ExpansionConcurrencyGuardDecision` — read ``.blocked``.
    """
    normalized_origin = _normalize_session_key(origin_session_key) or "main"
    trimmed_request_id = request_id.strip()

    with _state_lock:
        active_request_id = _active_request_id_by_origin_session_key.get(normalized_origin)
        if active_request_id and active_request_id != trimmed_request_id:
            # Captured for message-build outside the critical section.
            blocked_active_id = active_request_id
        else:
            blocked_active_id = None
            if not active_request_id:
                _active_request_id_by_origin_session_key[normalized_origin] = trimmed_request_id

    if blocked_active_id is not None:
        message = (
            f"{EXPANSION_CONCURRENCY_ERROR_CODE}: Another lcm_expand_query "
            f"delegation is already in flight for origin session "
            f"({normalized_origin}; activeRequestId={blocked_active_id}). "
        ) + _build_expansion_concurrency_recovery_guidance(normalized_origin)
        return _ConcurrencyGuardBlocked(
            blocked=True,
            code=EXPANSION_CONCURRENCY_ERROR_CODE,
            reason="origin_session_in_flight",
            message=message,
            request_id=trimmed_request_id,
            origin_session_key=normalized_origin,
        )

    return _ConcurrencyGuardAllowed(
        blocked=False,
        request_id=trimmed_request_id,
        origin_session_key=normalized_origin,
    )


def release_expansion_concurrency_slot(
    *,
    origin_session_key: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """Release the in-flight slot for ``origin_session_key`` if held.

    Mirrors TS ``releaseExpansionConcurrencySlot`` (lines 283-300).
    Releases only when the caller's ``request_id`` matches the slot's
    current holder — protects against a stale release stomping a fresh
    acquire from a different caller. Passing ``request_id=None``
    force-releases regardless of holder.

    Empty ``origin_session_key`` is a no-op (no slot to release).

    Args:
        origin_session_key: Origin session whose slot to release.
        request_id: Optional holder check. If provided and not matching
            the current slot holder, the release is a no-op.
    """
    normalized_origin = _normalize_session_key(origin_session_key)
    if not normalized_origin:
        return
    trimmed_request_id = request_id.strip() if isinstance(request_id, str) else None
    with _state_lock:
        active_request_id = _active_request_id_by_origin_session_key.get(normalized_origin)
        if not active_request_id:
            return
        if trimmed_request_id and active_request_id != trimmed_request_id:
            return
        _active_request_id_by_origin_session_key.pop(normalized_origin, None)


def record_expansion_delegation_telemetry(
    *,
    component: str,
    event: TelemetryEvent,
    request_id: str,
    expansion_depth: int,
    origin_session_key: str,
    session_key: Optional[str] = None,
    reason: Optional[str] = None,
    run_id: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Emit a structured delegated-expansion telemetry line.

    Mirrors TS ``recordExpansionDelegationTelemetry`` (lines 305-339).
    Increments the in-memory counter for ``event`` and emits a JSON
    payload prefixed by ``[lcm][expansion_delegation]``. ``start`` /
    ``success`` events log at INFO; ``block`` / ``timeout`` log at
    WARN. Counters are monotonic; only :func:`reset_for_tests` clears
    them.

    The TS source threads a ``deps`` object that carries a logger; the
    Python port accepts an optional ``logger`` kwarg with the
    module-level ``logging.getLogger("lcm.expansion")`` as default.
    This is a deliberate departure from the TS-shape — Python's stdlib
    logging is the natural seam and removes the need for a dependency
    bag in tests.

    Args:
        component: Source component (free-form, e.g.
            ``"lcm_expand_query"``).
        event: One of ``"start"``, ``"block"``, ``"timeout"``,
            ``"success"``.
        request_id: Expansion request id.
        expansion_depth: Depth at the time of the event.
        origin_session_key: Origin session key.
        session_key: Optional current session key (the delegated child).
        reason: Optional human-readable cause for ``block`` / ``timeout``.
        run_id: Optional Hermes run id.
        logger: Optional logger; defaults to ``lcm.expansion``.
    """
    with _state_lock:
        _telemetry_counters[event] = _telemetry_counters[event] + 1
        counter_snapshot = dict(_telemetry_counters)  # copy under lock

    normalized_session_key = _normalize_session_key(session_key) or None
    payload = {
        "component": component,
        "event": event,
        "requestId": request_id,
        "sessionKey": normalized_session_key,
        "expansionDepth": expansion_depth,
        "originSessionKey": origin_session_key,
        "reason": reason,
        "runId": run_id,
        "counters": counter_snapshot,
    }
    # ``separators=(",", ":")`` keeps the line compact (matches JS
    # ``JSON.stringify`` default no-space output).
    line = f"[lcm][expansion_delegation] {json.dumps(payload, separators=(',', ':'))}"
    log = logger if logger is not None else _LOG
    if event in ("start", "success"):
        log.info(line)
    else:
        log.warning(line)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def get_delegated_expansion_context_for_tests(
    session_key: str,
) -> Optional[DelegatedExpansionContext]:
    """Read the stamped context for ``session_key`` — TESTS ONLY.

    Mirrors TS ``getDelegatedExpansionContextForTests``. Returns
    ``None`` when no context is stamped for the (normalized) key.
    """
    key = _normalize_session_key(session_key)
    with _state_lock:
        return _delegated_context_by_session_key.get(key)


def get_expansion_delegation_telemetry_snapshot_for_tests() -> dict[TelemetryEvent, int]:
    """Snapshot the telemetry counters — TESTS ONLY.

    Returns a copy so callers can mutate the result without disturbing
    the live counters.
    """
    with _state_lock:
        return dict(_telemetry_counters)


def reset_for_tests() -> None:
    """Reset all state maps + telemetry counters — TESTS ONLY.

    Used by the pytest fixture in ``tests/tools/test_expansion_recursion_guard.py``
    to guarantee state isolation between tests. Production callers
    must never call this.
    """
    with _state_lock:
        _delegated_context_by_session_key.clear()
        _blocked_request_ids_by_session_key.clear()
        _active_request_id_by_origin_session_key.clear()
        _telemetry_counters["start"] = 0
        _telemetry_counters["block"] = 0
        _telemetry_counters["timeout"] = 0
        _telemetry_counters["success"] = 0
