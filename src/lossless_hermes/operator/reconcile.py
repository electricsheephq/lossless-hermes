"""``/lcm reconcile-session-keys`` — session-key merge operator (LCM v4.1 §2 / Group F.04).

Ports ``lossless-claw/src/operator/reconcile-session-keys.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 301 LOC TS → ~330 LOC Python).

The use case: pre-v4.1 conversations may have had NULL session_keys.
A.09 backfilled those to ``legacy:conv_<id>`` so cross-conv lookups
work, but each legacy thread remains in its OWN session bucket. An
operator (Eva) wants to merge several legacy threads into a single
logical session-key — e.g. ``legacy:conv_5, legacy:conv_8 →
'my-rebase-thread'`` — so future retrieval treats them as one
conversation history.

What this module does:

1. ``UPDATE conversations.session_key`` for every conversation matching
   one of the ``from`` keys to the new ``to`` key.
2. ``UPDATE summaries.session_key`` for the same set, so retrieval
   surfaces (which scope by session_key) see the merged history.
3. ``INSERT`` one audit row per CONVERSATION moved into
   ``lcm_session_key_audit``. (Schema constraint: ``conversation_id``
   is ``NOT NULL``, so we cannot use a single bulk audit row per
   ``from`` key — the per-conversation grain is also more useful for
   the ``/lcm undo-session-key-rekey <conv>`` reverse path.)

Refusal cases (raise :class:`ReconcileError`):

* ``to_session_key == "agent:main:main"`` without ``allow_main_session=True``.
  The main session_key is special — accidentally merging legacy work
  into it pollutes the operator's primary thread. Operator must opt
  in explicitly.
* ``from_session_keys`` is empty. (No-op would silently complete; we
  raise so operators notice typos.)
* Empty ``reason``. Audit trail is load-bearing — the next operator
  reading ``lcm_session_key_audit`` needs to know WHY each rekey
  happened.
* Multiple ACTIVE conversations span source + target. The
  ``conversations_active_session_key_idx`` UNIQUE partial index
  requires at most 1 active row per session_key — pre-check up-front
  and throw typed :class:`ReconcileError` with a workaround in the
  message.

Idempotency:

Re-running the same call once the data is already migrated is a
no-op for the UPDATE statements (no rows match the ``from`` keys
anymore) and writes ZERO new audit rows (because no conversations
moved). The function returns the empty result; this is safe.

### Atomicity guarantee — Wave-9 P1 TOCTOU fix

The active-conflict pre-check + the ``affected_convs`` snapshot run
INSIDE the ``BEGIN IMMEDIATE`` transaction. A concurrent
INSERT/UPDATE between snapshot and tx-acquire would have landed a
row that the UPDATE moves but the audit loop doesn't see, silently
dropping its audit-row → loss-of-undo on a destructive op (no way
to ``/lcm undo-session-key-rekey`` a conv that has no audit entry).
Mirror of the Wave-8 P1 ``run_soft_purge_atomic`` fix.

### Caller-side gating

**Owner-gating is NOT enforced inside this module** (per ADR-013).
The ``/lcm reconcile-session-keys`` slash command dispatcher
(``commands/reconcile.py``) and Hermes's upstream
``SlashAccessPolicy`` gate the surface — this module trusts that any
caller has already passed the policy gate. Direct imports from
non-CLI code MUST gate via ``ctx.senderIsOwner`` or equivalent
before invoking :func:`reconcile_session_keys`.

See:

* ``epics/08-cli-ops/08-05-reconcile-session-keys.md`` — this issue.
* ``docs/porting-guides/doctor-ops.md`` §"Operator modules" line 309 — signatures.
* ``docs/adr/013-owner-gating.md`` — caller-side gating, not handler-side.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-9 marker preserved.
* ``lossless-claw/src/operator/reconcile-session-keys.ts`` — TS source pinned
  at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger("lossless_hermes.operator.reconcile")


# ---------------------------------------------------------------------------
# Public surface — dataclasses + error type
# ---------------------------------------------------------------------------


ReconcileErrorKind = Literal[
    "no_from_keys",
    "missing_reason",
    "main_session_blocked",
    "active_conflict",
]


class ReconcileError(Exception):
    """Raised by :func:`reconcile_session_keys` on unsafe / invalid input.

    Ports the TS ``ReconcileError`` class (reconcile-session-keys.ts:74-86).
    The ``kind`` attribute disambiguates the four failure modes so callers
    (the ``/lcm reconcile-session-keys`` handler in
    ``commands/reconcile.py``) can render operator-facing messages:

    * ``"no_from_keys"`` — ``from_session_keys`` is empty. A no-op would
      silently complete; we raise so operators notice typos.
    * ``"missing_reason"`` — ``reason`` is empty or whitespace-only. The
      audit trail in ``lcm_session_key_audit.reason`` is load-bearing.
    * ``"main_session_blocked"`` — ``to_session_key == "agent:main:main"``
      without ``allow_main_session=True``. Eva's primary thread is too
      load-bearing for an accidental ``--to agent:main:main`` to be
      tolerated.
    * ``"active_conflict"`` — multiple ACTIVE conversations span source +
      target. The ``conversations_active_session_key_idx`` UNIQUE partial
      index requires at most 1 active row per session_key.

    Args:
        kind: One of :data:`ReconcileErrorKind`.
        message: Human-readable detail. Surfaced to operators in the
            ``/lcm reconcile-session-keys`` output.
    """

    def __init__(self, kind: ReconcileErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: ReconcileErrorKind = kind


@dataclass(frozen=True)
class ReconcileArgs:
    """Input shape for :func:`reconcile_session_keys`.

    Ports the TS ``ReconcileArgs`` interface (reconcile-session-keys.ts:48-63).

    Attributes:
        from_session_keys: Source session_keys to merge. Must be
            non-empty. The function raises ``ReconcileError("no_from_keys")``
            otherwise.
        to_session_key: Destination session_key. New value of
            ``conversations.session_key`` + ``summaries.session_key`` for
            every row matched by the source keys.
        reason: Required free-text reason. Recorded in
            ``lcm_session_key_audit.reason``. Empty / whitespace-only
            raises ``ReconcileError("missing_reason")``.
        allow_main_session: Override safety: allow
            ``to_session_key == "agent:main:main"``. Default ``False``
            (refuses).
        applied_by: Defaults to ``"operator"``. Recorded in
            ``lcm_session_key_audit.applied_by`` so we can distinguish
            operator-driven reconciles from migration backfills (the A.09
            step uses ``applied_by='migration'``).
    """

    from_session_keys: list[str]
    to_session_key: str
    reason: str
    allow_main_session: bool = False
    applied_by: str = "operator"


@dataclass(frozen=True)
class ReconcileResult:
    """Result of a successful :func:`reconcile_session_keys` call.

    Ports the TS ``ReconcileResult`` interface (reconcile-session-keys.ts:65-72).
    All fields are non-Optional — :func:`reconcile_session_keys` returns
    this only on the happy path; failures raise :class:`ReconcileError`.

    Attributes:
        conversations_moved: How many ``conversations`` rows were moved.
        summaries_moved: How many ``summaries`` rows were moved.
        audit_entries: How many ``lcm_session_key_audit`` rows were
            inserted (one per conversation moved). For an empty match,
            ``0`` (not ``None``).
    """

    conversations_moved: int
    summaries_moved: int
    audit_entries: int


@dataclass(frozen=True)
class ReconcileCandidate:
    """A legacy session_key candidate for reconciliation.

    Ports the TS ``ReconcileCandidate`` interface (reconcile-session-keys.ts:88-92).
    Surfaced by :func:`list_legacy_candidates` to the operator-facing
    ``/lcm reconcile-session-keys --list-candidates`` output.

    Attributes:
        session_key: The ``legacy:conv_*`` session_key candidate.
        conversation_count: How many ``conversations`` rows share this
            session_key.
        leaf_count: How many ``summaries`` rows with ``kind='leaf'``
            share this session_key.
    """

    session_key: str
    conversation_count: int
    leaf_count: int


# ---------------------------------------------------------------------------
# Public functions — list + reconcile
# ---------------------------------------------------------------------------


def list_legacy_candidates(db: sqlite3.Connection) -> list[ReconcileCandidate]:
    """List candidate ``legacy:conv_*`` session_keys that look mergeable.

    Ports the TS ``listLegacyCandidates`` (reconcile-session-keys.ts:258-282).
    For each candidate returns the session_key plus its conversation + leaf
    counts, so operators can decide which threads to combine.

    Sorted by ``conversation_count DESC`` so the chunkiest legacy threads
    surface first, then by ``session_key ASC`` for stable ordering when
    counts tie.

    Args:
        db: Open SQLite connection. Read-only; this function does NOT
            modify the DB and does NOT open a transaction (safe to
            interleave with any caller-held tx).

    Returns:
        List of :class:`ReconcileCandidate`. Empty list when no
        ``legacy:conv_*`` rows exist.
    """
    rows = db.execute(
        """
        SELECT c.session_key AS session_key,
               COUNT(DISTINCT c.conversation_id) AS conv_count,
               (SELECT COUNT(*) FROM summaries s
                 WHERE s.session_key = c.session_key AND s.kind = 'leaf') AS leaf_count
          FROM conversations c
          WHERE c.session_key LIKE 'legacy:conv_%'
          GROUP BY c.session_key
          ORDER BY conv_count DESC, c.session_key ASC
        """
    ).fetchall()
    return [
        ReconcileCandidate(
            session_key=row[0],
            conversation_count=row[1],
            leaf_count=row[2],
        )
        for row in rows
    ]


def reconcile_session_keys(
    db: sqlite3.Connection,
    args: ReconcileArgs,
) -> ReconcileResult:
    """Run the session-key reconcile.

    Ports the TS ``reconcileSessionKeys`` (reconcile-session-keys.ts:101-247).
    Validates the input (raises :class:`ReconcileError` on unsafe shapes),
    resolves the affected conversations, and runs the 3-step merge inside a
    single ``BEGIN IMMEDIATE`` transaction.

    Validation rules (mirror TS reconcile-session-keys.ts:105-122):

    * ``args.from_session_keys`` must be non-empty.
    * ``args.reason`` must be non-empty after :py:meth:`str.strip`.
    * If ``args.to_session_key == "agent:main:main"``, then
      ``args.allow_main_session`` must be ``True``.

    Atomicity guarantee:

    * The active-conflict pre-check + affected-conv snapshot + the 3
      UPDATE/INSERT steps run inside ONE ``BEGIN IMMEDIATE`` transaction
      (Wave-9 Agent #10 P1 TOCTOU fix preserved from TS). A concurrent
      ``INSERT INTO conversations`` between snapshot and tx-acquire
      cannot silently drop an audit row.

    Args:
        db: Open SQLite connection. MUST be in autocommit mode (no
            outer transaction); ``reconcile_session_keys`` opens its own
            ``BEGIN IMMEDIATE``.
        args: Full reconcile input — :class:`ReconcileArgs`.

    Returns:
        :class:`ReconcileResult` with the affected row counts. Empty
        match returns ``ReconcileResult(0, 0, 0)`` (NOT an error —
        operators re-running a reconcile against already-merged data
        should not get a spurious failure).

    Raises:
        ReconcileError: Input failed validation. ``kind`` indicates which
            rule fired; ``message`` describes the fix.
        sqlite3.Error: Underlying DB error during merge. ``ROLLBACK``
            is invoked before the exception propagates so no partial
            state lands.
    """
    # 1. Input validation — mirrors TS reconcile-session-keys.ts:105-122.
    if not args.from_session_keys or len(args.from_session_keys) == 0:
        raise ReconcileError(
            "no_from_keys",
            "[reconcile] from_session_keys must be non-empty",
        )
    if not args.reason or not args.reason.strip():
        raise ReconcileError(
            "missing_reason",
            "[reconcile] reason is required",
        )
    if args.to_session_key == "agent:main:main" and not args.allow_main_session:
        raise ReconcileError(
            "main_session_blocked",
            "[reconcile] refusing to write into agent:main:main without allow_main_session=True",
        )

    # 2. Atomic merge — pre-check + snapshot + 3 steps in one BEGIN IMMEDIATE.
    # LCM Wave-9 (2026-04-30): Agent #10 P1 TOCTOU fix — resolve the
    # active-conflict pre-check + affected_convs snapshot INSIDE the
    # BEGIN IMMEDIATE transaction so a concurrent INSERT/UPDATE between
    # snapshot and tx-acquire can't land a row the UPDATE moves but the
    # audit loop doesn't see (silently dropping its audit row → loss-of-
    # undo on a destructive op).
    # Original: lossless-claw/src/operator/reconcile-session-keys.ts:124-131.
    return _reconcile_atomic(db, args)


# ---------------------------------------------------------------------------
# Internals — atomic body
# ---------------------------------------------------------------------------


def _reconcile_atomic(
    db: sqlite3.Connection,
    args: ReconcileArgs,
) -> ReconcileResult:
    """Open ``BEGIN IMMEDIATE``, pre-check, snapshot, run merge, COMMIT.

    Ports the TS ``performReconcile`` (reconcile-session-keys.ts:134-236)
    plus the surrounding tx-management at lines 238-246. The
    active-conflict pre-check, affected-conv snapshot, and 3-step merge
    share one transaction (Wave-9 Agent #10 P1 TOCTOU fix preserved —
    see :func:`reconcile_session_keys` docstring). On any error,
    ``ROLLBACK`` is invoked before the exception propagates so no
    partial state survives.

    Empty-match path: when the snapshot returns ``[]`` AND no orphan
    summaries remain, we ``COMMIT`` an empty transaction and return
    ``ReconcileResult(0, 0, 0)``. This is NOT an error — operators
    re-running a reconcile against already-merged data get a clean
    empty result, not a crash. Orphan summaries (rows in ``summaries``
    whose session_key matches a ``from`` key but whose conversation_id
    points to a conversation with a different session_key) DO still get
    migrated (covered by ``test_orphan_summaries_still_migrated``).

    Args:
        db: Open SQLite connection in autocommit mode.
        args: Full reconcile input.

    Returns:
        :class:`ReconcileResult` — see :func:`reconcile_session_keys`.
    """
    placeholders = ",".join(["?"] * len(args.from_session_keys))
    from_keys_tuple = tuple(args.from_session_keys)

    db.execute("BEGIN IMMEDIATE")
    try:
        # ---- Active-conflict pre-check (inside tx — Wave-9 P1 fix) ----
        # The conversations.session_key UNIQUE partial index (active=1)
        # would fire mid-UPDATE with a raw SQLite error. Final review #5
        # fix: pre-check up-front and throw typed
        # ReconcileError("active_conflict") with a workaround in the
        # message.
        active_from_count = db.execute(
            f"""
            SELECT COUNT(*) FROM conversations
              WHERE session_key IN ({placeholders}) AND active = 1
            """,
            from_keys_tuple,
        ).fetchone()[0]
        active_to_count = db.execute(
            """
            SELECT COUNT(*) FROM conversations
              WHERE session_key = ? AND active = 1
            """,
            (args.to_session_key,),
        ).fetchone()[0]
        if active_from_count + active_to_count > 1:
            raise ReconcileError(
                "active_conflict",
                f"[reconcile] cannot merge {active_from_count} active "
                f"conversation(s) from {','.join(args.from_session_keys)} "
                f"into {args.to_session_key} (already has {active_to_count} "
                f"active) - the conversations.session_key UNIQUE-active "
                f"partial index requires at most 1 active per session_key. "
                f"Workaround: archive all but one conv first via "
                f"UPDATE conversations SET active=0, "
                f"archived_at=datetime('now') WHERE conversation_id=?, "
                f"then re-run reconcile.",
            )

        # ---- Snapshot affected conversations (inside tx) -------------
        affected_convs = db.execute(
            f"""
            SELECT conversation_id, session_key
              FROM conversations
              WHERE session_key IN ({placeholders})
            """,
            from_keys_tuple,
        ).fetchall()

        if len(affected_convs) == 0:
            # No matching conversations — but orphan summaries may still
            # exist (their conversation_id points to a conversation with
            # a different session_key, e.g. the conv was already
            # migrated but its summaries weren't). Fall through to the
            # summaries-only path in that case.
            orphan_summary_count = db.execute(
                f"""
                SELECT COUNT(*) FROM summaries
                  WHERE session_key IN ({placeholders})
                """,
                from_keys_tuple,
            ).fetchone()[0]
            if orphan_summary_count == 0:
                db.execute("COMMIT")
                return ReconcileResult(
                    conversations_moved=0,
                    summaries_moved=0,
                    audit_entries=0,
                )

        conversations_moved = 0
        summaries_moved = 0
        audit_entries = 0

        # ---- Step 1: UPDATE conversations.session_key ----------------
        if len(affected_convs) > 0:
            cursor = db.execute(
                f"""
                UPDATE conversations SET session_key = ?
                  WHERE session_key IN ({placeholders})
                """,
                (args.to_session_key, *args.from_session_keys),
            )
            conversations_moved = cursor.rowcount

        # ---- Step 2: UPDATE summaries.session_key --------------------
        cursor = db.execute(
            f"""
            UPDATE summaries SET session_key = ?
              WHERE session_key IN ({placeholders})
            """,
            (args.to_session_key, *args.from_session_keys),
        )
        summaries_moved = cursor.rowcount

        # ---- Step 3: INSERT one audit row per conversation moved -----
        # Schema constraint: lcm_session_key_audit.conversation_id is
        # NOT NULL → we cannot use a single bulk audit row per ``from``
        # key. Per-conversation grain is also the right granularity for
        # the ``/lcm undo-session-key-rekey <conv>`` reverse path.
        for conv_id, original_session_key in affected_convs:
            audit_id = f"reconcile_{int(time.time() * 1000)}_{conv_id}_{secrets.token_hex(3)}"
            db.execute(
                """
                INSERT INTO lcm_session_key_audit
                  (audit_id, conversation_id, original_session_key,
                   new_session_key, reason, applied_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    conv_id,
                    original_session_key,
                    args.to_session_key,
                    args.reason,
                    args.applied_by,
                ),
            )
            audit_entries += 1

        db.execute("COMMIT")
        return ReconcileResult(
            conversations_moved=conversations_moved,
            summaries_moved=summaries_moved,
            audit_entries=audit_entries,
        )
    except Exception:
        # Best-effort rollback. If the outer exception is "cannot
        # rollback (no transaction)" we don't want to mask the original
        # error.
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


__all__ = [
    "ReconcileArgs",
    "ReconcileCandidate",
    "ReconcileError",
    "ReconcileErrorKind",
    "ReconcileResult",
    "list_legacy_candidates",
    "reconcile_session_keys",
]
