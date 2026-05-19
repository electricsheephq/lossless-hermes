"""Synthesis audit-row writes + retention sweeps — LCM v4.1 (issue 07-09).

The forensic audit trail for every LLM call dispatched by
:mod:`lossless_hermes.synthesis.dispatch` (issue 07-05). This module
centralises three concerns the dispatcher previously inlined:

* **Insert-before-call lifecycle.** A ``status='started'`` row is
  written to :sql:`lcm_synthesis_audit` BEFORE the LLM is invoked; an
  UPDATE to ``'completed'`` or ``'failed'`` follows once the call
  finishes. The pattern guarantees a forensic record survives a process
  crash between the LLM call and the row update — operators can sweep
  orphan ``'started'`` rows older than 1 h later.
* **Typed insert-failure surface.** :func:`insert_audit_started` wraps
  the INSERT in a ``try/except`` so FK violations (unknown
  ``prompt_id`` / ``target_summary_id``) and the
  ``target_summary_id IS NOT NULL OR target_cache_id IS NOT NULL``
  CHECK violation surface as
  :exc:`SynthesisDispatchError("audit_insert_failure")` BEFORE any LLM
  spend (LCM Wave-9 Group D adversarial Gap 4).
* **Retention sweeps.** Two DELETEs that hit the partial indexes
  ``lcm_synthesis_audit_started_gc_idx`` (orphan ``'started'`` >1 h)
  and ``lcm_synthesis_audit_completed_gc_idx`` (terminal rows >N days).
  Tunable via ``LCM_AUDIT_RETENTION_DAYS`` (default 30 — see ADR-023).

### Truncation

:func:`truncate_for_audit` clamps both ``pass_input_truncated`` and
``pass_output`` to :data:`AUDIT_MAX_LEN` (8000 chars) with a
``"…(truncated)"`` marker. Full inputs / outputs are NOT retained — see
the open decision in ``synthesis.md`` §"Open Decisions §3" re: future
opt-in body logging via ``LCM_AUDIT_LOG_BODIES``.

### Audit-ID format

:func:`generate_audit_id` returns ``aud_<6 hex chars>`` from
:func:`secrets.token_hex(3)`. 24 bits of entropy is sufficient: PK
collisions are bounded by the per-pass-session row count (≤6 for
yearly's best-of-N=5 + judge); a single session would need to spawn
~4000 audit rows before birthday-paradox collisions become probable.

### Cost accounting

``cost_usd_cents`` is stored as INTEGER. The injected LLM-call adapter
computes cents from token counts × per-model rates and surfaces the
value on :class:`LlmCallResult.cost_cents`. This module trusts that
number. Future schema work (see synthesis.md §"Remaining 5% risk" item
1) may add separate ``prompt_tokens`` + ``completion_tokens`` columns
so rate-table updates are recomputable on demand; not in v0.1.

### `hallucination_flag` is NOT a column

The verify-fidelity pass produces a per-call hallucination flag that
the dispatcher surfaces on :class:`SynthesizeResult.hallucination_flagged`
— it is NOT persisted on the audit row. The detection regex lives in
issue 07-05 (LCM Wave-4 Auditor #5 P0 fix).

### Source pin

* TS canonical: ``lossless-claw/src/synthesis/dispatch.ts`` (commit
  ``1f07fbd`` on branch ``pr-613``):
  - lines 395–453 — ``runPassWithAudit`` (the lifecycle wrapper that
    calls these helpers in the TS port).
  - lines 467–536 — ``insertAuditRow`` + ``updateAuditRow``.
  - lines 809–811 — ``truncateForAudit``.
* TS canonical (sweep): ``lossless-claw/src/operator/health.ts`` lines
  286–353 inline ``SELECT COUNT(*)`` for partial-index health metrics;
  the corresponding ``DELETE`` sweep called by ``/lcm health`` and
  doctor-ops lives in this module (the TS source emits the equivalent
  ``DELETE`` from the inline GC inside ``dispatchSynthesis`` itself).
* Spec: ``epics/07-entity-synthesis/07-09-synthesis-audit.md``.
* Schema: :mod:`lossless_hermes.db.migration`
  (``_SQL_TABLE_LCM_SYNTHESIS_AUDIT`` + partial indexes
  ``lcm_synthesis_audit_started_gc_idx`` and
  ``lcm_synthesis_audit_completed_gc_idx``).
* ADR-023: ``docs/adr/023-config-delivery.md`` —
  ``LCM_AUDIT_RETENTION_DAYS`` env override.
* ADR-029: ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment
  format.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lossless_hermes.synthesis.types import PassKind

__all__ = [
    "AUDIT_ID_PREFIX",
    "AUDIT_MAX_LEN",
    "AUDIT_TRUNCATED_MARKER",
    "DEFAULT_RETENTION_DAYS",
    "ENV_RETENTION_DAYS",
    "LAST_ERROR_MAX_LEN",
    "AuditCompletedResult",
    "AuditInsertContext",
    "generate_audit_id",
    "insert_audit_started",
    "resolve_retention_days",
    "sweep_orphan_audit_starts",
    "sweep_terminal_audit_rows",
    "truncate_for_audit",
    "update_audit_completed",
    "update_audit_failed",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Audit-row text-field hard cap (chars). Pass inputs + outputs longer
#: than this are truncated with a ``"…(truncated)"`` marker. Full inputs
#: / outputs are NOT retained on the audit row.
#:
#: TS source: ``dispatch.ts:809-810`` (``maxLen = 8000``).
AUDIT_MAX_LEN: int = 8000

#: Suffix appended to truncated audit-row text. The single Unicode
#: ellipsis (U+2026, ``"…"``) matches the TS source verbatim — do NOT
#: replace with the ASCII ``"..."`` triple-dot (would silently change
#: 1 char to 3 in every truncated row).
AUDIT_TRUNCATED_MARKER: str = "…(truncated)"

#: PK prefix for audit-row IDs. ``aud_<6 hex>`` total = 10 chars,
#: storing ~24 bits of entropy. Sufficient given per-pass-session row
#: count is bounded (≤6 for yearly best-of-N=5 + judge).
AUDIT_ID_PREFIX: str = "aud_"

#: ``last_error`` column cap. Long traceback bodies leak PII + bloat
#: the table; 500 chars retains the typed-error head + first frame.
#: TS source does not clamp; the Python port adds the cap to mitigate
#: stack-trace PII exposure per the audit AC checklist.
LAST_ERROR_MAX_LEN: int = 500

#: Default retention window (days) for terminal (``'completed'`` /
#: ``'failed'``) audit rows. Tunable via :data:`ENV_RETENTION_DAYS`.
DEFAULT_RETENTION_DAYS: int = 30

#: Env var name for the retention-window override. See ADR-023
#: §"Config delivery" for the env-driven config surface.
ENV_RETENTION_DAYS: str = "LCM_AUDIT_RETENTION_DAYS"


# ---------------------------------------------------------------------------
# Audit-ID generation + truncation
# ---------------------------------------------------------------------------


def generate_audit_id() -> str:
    """Return a fresh ``aud_<6 hex>`` audit-row ID.

    Uses :func:`secrets.token_hex(3)` for cryptographic-quality
    randomness (no downside, no extra cost compared to
    :mod:`random`). Format matches the AC checklist in issue 07-09.

    Returns:
        A string shaped like ``"aud_a1b2c3"``.
    """

    return f"{AUDIT_ID_PREFIX}{secrets.token_hex(3)}"


def truncate_for_audit(s: str, max_len: int = AUDIT_MAX_LEN) -> str:
    """Truncate a string to ``max_len`` chars with ``"…(truncated)"`` marker.

    Used by both pass_input + pass_output recording. Full inputs / outputs
    are not retained on the audit row.

    Mirrors the TS ``truncateForAudit`` at ``dispatch.ts:809-811``.

    Args:
        s: The string to truncate.
        max_len: Cap length. Default :data:`AUDIT_MAX_LEN` (8000 chars).

    Returns:
        Either ``s`` unchanged (if shorter than ``max_len``) or
        ``s[:max_len] + "…(truncated)"``.
    """

    if len(s) > max_len:
        return s[:max_len] + AUDIT_TRUNCATED_MARKER
    return s


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AuditInsertContext:
    """Context for one ``status='started'`` row insert.

    Threads the FK fields + the (already-truncated) input through to
    :func:`insert_audit_started` without forcing positional argument
    ordering on the caller.

    Mirrors the TS ``AuditRowFields`` interface at
    ``dispatch.ts:455-465`` plus the truncated input from
    ``PassAuditCtx``.

    Attributes:
        pass_session_id: Session ID shared across ALL passes of one
            logical synthesis attempt (LCM Wave-9 Group D Gap 2 —
            monthly: ``[single, verify_fidelity]``; yearly:
            ``[N× single, 1× judge]``). NOT suffixed per pass.
        target_summary_id: FK into :sql:`summaries`. Exactly one of
            this / :attr:`target_cache_id` MUST be non-``None``
            (CHECK constraint).
        target_cache_id: FK into :sql:`lcm_synthesis_cache`. Exactly
            one of this / :attr:`target_summary_id` MUST be
            non-``None``.
        prompt_id: FK into :sql:`lcm_prompt_registry`.
        pass_kind: The pass kind enum value (``single`` /
            ``verify_fidelity`` / ``best_of_n_judge``).
        pass_input_truncated: The LLM input, already passed through
            :func:`truncate_for_audit`. Stored verbatim.
        model_used: The model name as REQUESTED (the adapter may
            substitute on UPDATE; this column captures the initial
            choice).
    """

    pass_session_id: str
    target_summary_id: str | None
    target_cache_id: str | None
    prompt_id: str
    pass_kind: PassKind
    pass_input_truncated: str
    model_used: str


@dataclass(slots=True)
class AuditCompletedResult:
    """Result fields for a ``status='completed'`` UPDATE.

    Mirrors the relevant subset of TS ``LlmCallResult`` that the audit
    row stores; field names match the audit columns (NOT the LLM
    result's ``actualModel`` / ``costCents``).

    Attributes:
        pass_output: The LLM output, already passed through
            :func:`truncate_for_audit`.
        model_used: The model the adapter actually used (may differ
            from the model REQUESTED on insert — e.g. provider-side
            fallback).
        latency_ms: Wall-clock latency observed by the caller. Stored
            as INTEGER (caller passes a rounded value).
        cost_cents: USD cents (rounded), if the adapter knew the
            token-count × rate. ``None`` if not known.
    """

    pass_output: str
    model_used: str
    latency_ms: int
    cost_cents: int | None


# ---------------------------------------------------------------------------
# Row writes
# ---------------------------------------------------------------------------


def insert_audit_started(
    conn: sqlite3.Connection,
    audit_id: str,
    ctx: AuditInsertContext,
) -> None:
    """Insert the ``status='started'`` audit row BEFORE the LLM call.

    This is the half of the insert-before-call lifecycle that
    guarantees a forensic record exists if the process crashes between
    LLM invocation and ack. Orphan ``'started'`` rows older than 1 h
    are swept by :func:`sweep_orphan_audit_starts`.

    The caller MUST then either:

    * call :func:`update_audit_completed` after a successful LLM call, or
    * call :func:`update_audit_failed` after a thrown LLM error.

    The CHECK constraint
    ``target_summary_id IS NOT NULL OR target_cache_id IS NOT NULL``
    is enforced by SQLite; this helper passes both nullable but raises
    :exc:`ValueError` up-front if BOTH are ``None`` so the caller gets
    a clearer error than the raw SQLite CHECK violation.

    Note: the spec says FK / CHECK violations should surface as
    :exc:`SynthesisDispatchError("audit_insert_failure")` — that
    wrapping is the dispatcher's job (it catches
    :exc:`sqlite3.DatabaseError` from this call). This helper raises
    the raw SQLite error so the dispatcher can wrap it with full
    context (LCM Wave-9 Group D adversarial Gap 4).

    Mirrors the TS ``insertAuditRow`` at ``dispatch.ts:467-489``.

    Args:
        conn: Open :class:`sqlite3.Connection`. Caller controls
            transaction state.
        audit_id: The PK to insert. Typically from
            :func:`generate_audit_id`.
        ctx: :class:`AuditInsertContext` with the FK + content fields.

    Raises:
        ValueError: If BOTH ``ctx.target_summary_id`` and
            ``ctx.target_cache_id`` are ``None`` (would trip the
            CHECK constraint).
        sqlite3.DatabaseError: On FK violation (unknown ``prompt_id``,
            ``target_summary_id``, or ``target_cache_id``). The
            dispatcher is responsible for catching this and re-raising
            as :exc:`SynthesisDispatchError("audit_insert_failure")`.
    """

    if ctx.target_summary_id is None and ctx.target_cache_id is None:
        raise ValueError(
            "[synthesis.audit] insert_audit_started: at least one of "
            "target_summary_id / target_cache_id is required "
            "(lcm_synthesis_audit CHECK constraint requires one of "
            "them set)"
        )
    conn.execute(
        "INSERT INTO lcm_synthesis_audit"
        " (audit_id, pass_session_id, target_summary_id, target_cache_id, prompt_id,"
        "  pass_kind, pass_input_truncated, status, model_used)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            audit_id,
            ctx.pass_session_id,
            ctx.target_summary_id,
            ctx.target_cache_id,
            ctx.prompt_id,
            ctx.pass_kind,
            ctx.pass_input_truncated,
            "started",
            ctx.model_used,
        ),
    )


def update_audit_completed(
    conn: sqlite3.Connection,
    audit_id: str,
    result: AuditCompletedResult,
) -> None:
    """Update an audit row to ``status='completed'``.

    Writes ``status``, ``pass_output``, ``model_used``, ``latency_ms``,
    and (if known) ``cost_usd_cents``. The ``model_used`` column is
    rewritten in case the adapter substituted a different model from
    the one requested on insert.

    Mirrors the TS ``updateAuditRow(auditId, { status: "completed", ... })``
    call at ``dispatch.ts:438-444``.

    Args:
        conn: Open :class:`sqlite3.Connection`. Caller controls
            transaction state.
        audit_id: The PK of the row to update (from
            :func:`insert_audit_started`).
        result: :class:`AuditCompletedResult` with the post-LLM fields.
            ``result.cost_cents`` may be ``None`` if the adapter didn't
            return a token-count breakdown.
    """

    sets: list[str] = [
        "status = ?",
        "pass_output = ?",
        "model_used = ?",
        "latency_ms = ?",
    ]
    args: list[str | int] = [
        "completed",
        result.pass_output,
        result.model_used,
        result.latency_ms,
    ]
    if result.cost_cents is not None:
        sets.append("cost_usd_cents = ?")
        args.append(result.cost_cents)
    args.append(audit_id)
    conn.execute(
        f"UPDATE lcm_synthesis_audit SET {', '.join(sets)} WHERE audit_id = ?",
        args,
    )


def update_audit_failed(
    conn: sqlite3.Connection,
    audit_id: str,
    err: str,
    *,
    latency_ms: int | None = None,
) -> None:
    """Update an audit row to ``status='failed'``.

    Writes ``status='failed'`` and ``last_error`` (truncated to
    :data:`LAST_ERROR_MAX_LEN` to mitigate stack-trace PII exposure
    and table bloat). If the caller observed wall-clock latency before
    the LLM error surfaced, pass it via ``latency_ms`` for telemetry.

    Mirrors the TS ``updateAuditRow(auditId, { status: "failed", ... })``
    call at ``dispatch.ts:425-429``. The TS source does NOT cap
    ``last_error``; the Python port adds the cap to mitigate PII
    exposure per the issue 07-09 AC checklist.

    Args:
        conn: Open :class:`sqlite3.Connection`. Caller controls
            transaction state.
        audit_id: The PK of the row to update (from
            :func:`insert_audit_started`).
        err: The error message. Truncated to
            :data:`LAST_ERROR_MAX_LEN` chars.
        latency_ms: Optional wall-clock latency to record (only if the
            adapter observed the failure mid-call; ``None`` for
            pre-call errors).
    """

    sets: list[str] = ["status = ?", "last_error = ?"]
    args: list[str | int] = ["failed", err[:LAST_ERROR_MAX_LEN]]
    if latency_ms is not None:
        sets.append("latency_ms = ?")
        args.append(latency_ms)
    args.append(audit_id)
    conn.execute(
        f"UPDATE lcm_synthesis_audit SET {', '.join(sets)} WHERE audit_id = ?",
        args,
    )


# ---------------------------------------------------------------------------
# Retention sweeps
# ---------------------------------------------------------------------------


def sweep_orphan_audit_starts(
    conn: sqlite3.Connection,
    *,
    cutoff_minutes: int = 60,
) -> int:
    """Delete orphan ``status='started'`` rows older than the cutoff.

    These rows correspond to LLM calls where the process crashed
    between :func:`insert_audit_started` and the matching
    :func:`update_audit_completed` / :func:`update_audit_failed`. The
    forensic record is preserved for ``cutoff_minutes`` (default
    60 minutes); after that the row is considered abandoned and
    deleted.

    Uses the partial index ``lcm_synthesis_audit_started_gc_idx``
    (declared in :mod:`lossless_hermes.db.migration` —
    ``WHERE status = 'started'``) so the sweep is O(log n) on the
    orphan-row count rather than O(n) on the full table.

    Called by Epic 06 ``/lcm health`` / doctor-ops.

    Args:
        conn: Open :class:`sqlite3.Connection`. Caller controls
            transaction state.
        cutoff_minutes: Age in minutes beyond which a ``'started'``
            row is considered abandoned. Default 60.

    Returns:
        The count of rows deleted.
    """

    cur = conn.execute(
        "DELETE FROM lcm_synthesis_audit"
        " WHERE status = 'started'"
        f"   AND ran_at < datetime('now', '-{int(cutoff_minutes)} minutes')"
    )
    return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0


def sweep_terminal_audit_rows(
    conn: sqlite3.Connection,
    *,
    retention_days: int | None = None,
) -> int:
    """Delete terminal (``'completed'`` / ``'failed'``) rows older than retention.

    Uses the partial index ``lcm_synthesis_audit_completed_gc_idx``
    (declared in :mod:`lossless_hermes.db.migration` —
    ``WHERE status IN ('completed', 'failed')``) so the sweep is
    O(log n) on the terminal-row count rather than O(n) on the full
    table.

    Resolution order for the retention window:

    1. ``retention_days`` argument if not ``None``;
    2. otherwise :func:`resolve_retention_days` reads the
       :data:`ENV_RETENTION_DAYS` env var;
    3. falls back to :data:`DEFAULT_RETENTION_DAYS` (30) if unset.

    Called by Epic 06 ``/lcm health`` / doctor-ops.

    Args:
        conn: Open :class:`sqlite3.Connection`. Caller controls
            transaction state.
        retention_days: Override the env-resolved retention window.
            Default ``None`` (consult env / fallback to 30).

    Returns:
        The count of rows deleted.
    """

    days = retention_days if retention_days is not None else resolve_retention_days()
    cur = conn.execute(
        "DELETE FROM lcm_synthesis_audit"
        " WHERE status IN ('completed', 'failed')"
        f"   AND ran_at < datetime('now', '-{int(days)} days')"
    )
    return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0


def resolve_retention_days() -> int:
    """Resolve the terminal-row retention window in days.

    Reads :data:`ENV_RETENTION_DAYS` (``LCM_AUDIT_RETENTION_DAYS``) at
    call time; falls back to :data:`DEFAULT_RETENTION_DAYS` (30) if
    unset, empty, or non-numeric.

    Per ADR-023 §"Config delivery", env-driven overrides for
    operator-tunable knobs are honored at call time (not import time)
    so tests can monkeypatch and CI overrides land without process
    restart.

    Returns:
        A positive integer day count.
    """

    raw = os.environ.get(ENV_RETENTION_DAYS, "").strip()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_RETENTION_DAYS
    return parsed if parsed > 0 else DEFAULT_RETENTION_DAYS
