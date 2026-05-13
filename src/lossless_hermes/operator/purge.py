"""``/lcm purge`` — soft-suppression cascade (LCM v4.1 §10).

Ports ``lossless-claw/src/operator/purge.ts`` (LCM commit ``1f07fbd`` on
branch ``pr-613``, 390 LOC TS → ~430 LOC Python).

The operator's hard-forget surface: soft-suppress matched leaf summaries
(set ``suppressed_at`` + ``suppress_reason``) and cascade the suppression
through the 5 dependent surfaces so no agent-visible read path can
resurface the content. **Not a hard delete** — the rows stay in the DB,
only ``suppressed_at`` is set; downstream read paths (Epic 03 assembler,
Epic 05 embeddings, Epic 06 tools, Epic 07 entity coreference) filter on
the ``suppressed_at IS NULL`` invariant.

The full cascade runs inside one ``BEGIN IMMEDIATE`` transaction (per
doctor-ops.md §"runPurge SOFT SUPPRESSION" lines 261-268):

1. ``UPDATE summaries SET suppressed_at = datetime('now'), suppress_reason
   = ?`` for matched leaf IDs. This UPDATE fires the per-model vec0
   trigger ``lcm_embed_suppress_<slug>`` (Epic 05 ``ensureEmbeddingsTable``)
   which mirrors ``suppressed=1`` to the per-model vec0 metadata table,
   excluding suppressed embeddings from semantic search automatically.
2. ``UPDATE summaries SET contains_suppressed_leaves = 1`` for condensed
   summaries whose ``summary_parents.parent_summary_id`` is one of the
   suppressed leaves — flags them for idle rebuild.
3. ``DELETE FROM context_items WHERE item_type='summary' AND summary_id
   IN (...)`` — removes the assembler's pointer so the suppressed summary
   cannot be re-emitted into the prompt.
4. ``DELETE FROM context_items WHERE item_type='message' AND message_id
   IN (SELECT message_id FROM summary_messages WHERE summary_id IN (...))``
   — cuts the message-level pointer for the same reason.
5. ``UPDATE messages SET suppressed_at = datetime('now')`` for messages
   linked via ``summary_messages`` to suppressed leaves — **gated by
   ``NOT EXISTS`` on any non-suppressed referencing summary outside the
   purge set** (Wave-7 Auditor #14 P0-2 fix), so a message shared with a
   non-purged leaf is not orphaned.
6. ``DELETE FROM lcm_synthesis_cache WHERE cache_id IN (SELECT DISTINCT
   cache_id FROM lcm_cache_leaf_refs WHERE leaf_summary_id IN (...))`` —
   invalidates rebuildable synthesis caches that referenced the
   suppressed leaves. (The cache schema's ``ON DELETE CASCADE`` only
   fires on hard DELETE; soft suppression must do this explicitly.)

The hard-delete ``mode='immediate'`` (with rebuild-worker drainer of
``lcm_purge_rebuild_queue``) was **removed in the first-principles pass
(2026-05-06)** — the drainer worker (~20-40h work, HIGH risk to
assemble-pyramid invariants) was never built. Implementation + queue
schema preserved in upstream draft PR #616. :func:`run_purge` always
returns ``mode="soft"``.

**Soft purge is agent-visible suppression ONLY** — the leaf row, message
row, and any embedding metadata stay in the DB. SQL ``VACUUM`` alone
does NOT byte-delete the suppressed content because the rows still
exist. For GDPR-compliant byte erasure, the operator must run raw
``DELETE FROM messages/summaries WHERE summary_id IN (...)`` followed by
``VACUUM`` out-of-band (operator-only manual SQL).

### Audit divergence from issue spec

The issue spec (``epics/08-cli-ops/08-04-purge-soft-suppression.md``
line 45) mentions writing an audit row to ``lcm_session_key_audit``. The
TS source ``operator/purge.ts`` does NOT write such a row — and the
``lcm_session_key_audit`` schema (``conversation_id NOT NULL``,
``new_session_key NOT NULL``) is shaped for the reconcile-session-keys
flow, not purge. The TS source records the audit trail via
``summaries.suppress_reason`` per affected leaf, plus the in-memory
``purge_session_id`` returned to the caller. This Python port matches
the TS source 1:1 per CLAUDE.md "1:1 source-to-Python port" mandate and
ADR-029 §"Known Wave-N fixes" (the cascade IS the audit trail). Future
operators wanting a separate purge-audit table should propose a new ADR
with a purpose-built schema (action='purge', affected_count, host,
applied_at) rather than reuse ``lcm_session_key_audit``.

### Caller-side gating

**Owner-gating is NOT enforced inside this module** (per ADR-013). The
``/lcm purge`` slash command dispatcher (``commands/purge.py``) and
Hermes's upstream ``SlashAccessPolicy`` gate the surface — this module
trusts that any caller has already passed the policy gate. Direct
imports from non-CLI code MUST gate via ``ctx.senderIsOwner`` or
equivalent before invoking :func:`run_purge`.

See:

* ``epics/08-cli-ops/08-04-purge-soft-suppression.md`` — this issue.
* ``docs/porting-guides/doctor-ops.md`` §"runPurge SOFT SUPPRESSION"
  (lines 259-270) — full cascade spec.
* ``docs/adr/013-owner-gating.md`` — caller-side gating, not handler-side.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comments preserved
  per ADR-029. This module contains Wave-1, Wave-2, Wave-7, Wave-8
  markers.
* ``lossless-claw/src/operator/purge.ts`` — TS source pinned at commit
  ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

logger = logging.getLogger("lossless_hermes.operator.purge")


# ---------------------------------------------------------------------------
# Public surface — dataclasses + error type
# ---------------------------------------------------------------------------


PurgeErrorKind = Literal["no_criteria", "main_session_blocked", "missing_reason"]


class PurgeError(Exception):
    """Raised by :func:`run_purge` on unsafe / invalid input.

    Ports the TS ``PurgeError`` class (purge.ts:97-108). The ``kind``
    attribute disambiguates the three failure modes so callers (the
    ``/lcm purge`` handler in ``commands/purge.py``) can render
    operator-facing messages:

    * ``"missing_reason"`` — ``reason`` is empty or whitespace-only;
      ``summaries.suppress_reason`` would be ``NULL`` which loses the
      audit trail.
    * ``"no_criteria"`` — no scope criterion (``summary_ids``,
      ``session_key``, ``since``, ``before``, ``min_token_count``) was
      provided. Without one, the resolver would match every leaf in the
      DB — accidentally purging an entire corpus is a hazard worth a
      hard refuse.
    * ``"main_session_blocked"`` — ``session_key == "agent:main:main"``
      without ``allow_main_session=True``. Eva's primary thread is too
      load-bearing for an accidental ``--session-key agent:main:main``
      to be tolerated.

    Args:
        kind: One of :data:`PurgeErrorKind`.
        message: Human-readable detail. Surfaced to operators in the
            ``/lcm purge`` output.
    """

    def __init__(self, kind: PurgeErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind: PurgeErrorKind = kind


@dataclass(frozen=True)
class PurgeCriteria:
    """The scope criteria for a purge — one of these MUST be set.

    Ports the TS ``PurgeCriteria`` interface (purge.ts:49-60). Mutually
    permissive — the resolver combines all set fields with AND when
    using the range-purge path; the ``summary_ids`` path takes priority
    and ignores the other fields when present.

    Attributes:
        summary_ids: Explicit list of summary IDs to target. When set,
            the resolver filters to ``kind='leaf' AND suppressed_at IS
            NULL`` and returns only the matching IDs; other criteria are
            ignored. Operator mistakes (typos, non-existent IDs, condensed
            IDs, already-suppressed IDs) silently filter out — see
            ``test_explicit_summaryids_only_valid_leaf_ids``.
        session_key: Range purge: all leaves in this session_key.
            Required to be ``"agent:main:main"`` only when
            :attr:`PurgeOptions.allow_main_session` is ``True``.
        since: Range purge: only leaves with ``created_at >= since``.
            Stored as ISO-8601 UTC string (``datetime.isoformat()``).
        before: Range purge: only leaves with ``created_at < before``.
        min_token_count: Range purge: only leaves with
            ``token_count >= min_token_count``.
    """

    summary_ids: list[str] | None = None
    session_key: str | None = None
    since: datetime | None = None
    before: datetime | None = None
    min_token_count: int | None = None


@dataclass(frozen=True)
class PurgeOptions:
    """Full input shape for :func:`run_purge` — criteria + reason + flags.

    Ports the TS ``PurgeOptions`` interface (purge.ts:62-86). Composes
    :class:`PurgeCriteria` plus the two mandatory non-criteria fields
    (``reason``, ``allow_main_session``).

    Attributes:
        reason: Free-text reason. **Required** (no default). Recorded in
            ``summaries.suppress_reason`` for the affected leaves so the
            audit trail survives indefinitely (until the DB is dropped).
        criteria: The scope criteria (see :class:`PurgeCriteria`). At
            least one field must be set; :func:`run_purge` raises
            ``PurgeError("no_criteria")`` otherwise.
        allow_main_session: Override safety: allow purging the entire
            ``agent:main:main`` session. Default ``False`` (refuses).
    """

    reason: str
    criteria: PurgeCriteria = field(default_factory=PurgeCriteria)
    allow_main_session: bool = False


@dataclass(frozen=True)
class PurgeResult:
    """Result of a successful :func:`run_purge` call.

    Ports the TS ``PurgeResult`` interface (purge.ts:88-95). All fields
    are non-Optional — :func:`run_purge` returns this only on the happy
    path; failures raise :class:`PurgeError`.

    Attributes:
        affected_leaf_ids: Summary IDs that were actually suppressed.
            For an empty match, an empty list (not ``None``). Sorted in
            DB-return order (which is undefined per SQLite — the
            ``test_*_returns`` tests should compare as sets where order
            matters).
        purge_session_id: In-memory traceability token. Format:
            ``f"purge_{int(time.time() * 1000)}_{6-hex-chars}"`` — used
            by the ``/lcm purge`` handler in its output and by tests to
            disambiguate concurrent purge calls. NOT persisted; if you
            need a durable audit trail, see ``summaries.suppress_reason``
            instead.
        mode: Always ``"soft"`` (Literal type). The TS
            ``mode='immediate'`` hard-delete path was removed in the
            first-principles pass (2026-05-06); we keep the field for
            output-shape parity with the TS ``PurgeResult``.
    """

    affected_leaf_ids: list[str]
    purge_session_id: str
    mode: Literal["soft"] = "soft"


# ---------------------------------------------------------------------------
# Public functions — preview + apply
# ---------------------------------------------------------------------------


def preview_purge_affected(
    db: sqlite3.Connection,
    criteria: PurgeCriteria,
) -> int:
    """Dry-run count: how many leaves would :func:`run_purge` affect?

    Ports the TS ``previewPurgeAffected`` (purge.ts:198-200). Uses the
    EXACT same resolver as :func:`run_purge` so the dry-run count matches
    the apply count.

    Wave-2 Auditor #6 fix BUG-2 + BUG-3 (preserved from TS): the previous
    dry-run implementation used its own ``WHERE`` clauses
    (``datetime(created_at) >= datetime(?)`` while runPurge used raw
    ``created_at >= ?``); edge cases (timezone offsets, microseconds)
    gave divergent counts. The single-resolver design is the fix.

    Args:
        db: Open SQLite connection. Caller controls transaction state;
            this function does NOT open a transaction (it's a read-only
            count, safe to interleave with any caller-held tx).
        criteria: The scope criteria. NO ``reason`` field — preview is
            criteria-only.

    Returns:
        Integer count of leaves that :func:`run_purge` with the same
        criteria + a non-empty reason WOULD suppress. ``0`` if no leaves
        match.

    Does NOT modify the DB.
    """
    return len(_resolve_target_leaf_ids(db, criteria))


def run_purge(db: sqlite3.Connection, opts: PurgeOptions) -> PurgeResult:
    """Run an operator-driven purge.

    Ports the TS ``runPurge`` (purge.ts:122-153). Validates the input
    (raises :class:`PurgeError` on unsafe shapes), resolves the target
    leaf IDs, and runs the 6-step soft-suppression cascade inside a
    single ``BEGIN IMMEDIATE`` transaction.

    Validation rules (mirror TS purge.ts:124-141):

    * ``opts.reason`` must be non-empty after :py:meth:`str.strip`. An
      empty reason loses the audit trail in ``summaries.suppress_reason``.
    * At least one ``opts.criteria`` field must be set. An empty
      criteria would match every leaf in the DB — never desired.
    * If ``opts.criteria.session_key == "agent:main:main"``, then
      ``opts.allow_main_session`` must be ``True``. Eva's primary thread
      is too load-bearing for an accidental purge.

    Atomicity guarantee:

    * The resolve + 6 cascade steps run inside ONE ``BEGIN IMMEDIATE``
      transaction (Wave-8 Auditor #13-18 E-P1 fix preserved from TS).
      A concurrent ``/lcm purge`` or any other ``summaries.suppressed_at``
      writer cannot change the leaf set between resolve and UPDATE.

    Args:
        db: Open SQLite connection. MUST be in autocommit mode (no
            outer transaction); ``run_purge`` opens its own
            ``BEGIN IMMEDIATE``.
        opts: Full purge input — :class:`PurgeOptions`.

    Returns:
        :class:`PurgeResult` with ``mode="soft"``, the affected leaf IDs,
        and the in-memory ``purge_session_id``. Empty match returns an
        empty ``affected_leaf_ids`` list (NOT an error — operators
        re-running a purge against an already-purged set should not get
        a spurious failure).

    Raises:
        PurgeError: Input failed validation. ``kind`` indicates which
            rule fired; ``message`` describes the fix.
        sqlite3.Error: Underlying DB error during cascade. ``ROLLBACK``
            is invoked before the exception propagates so no partial
            state lands.
    """
    # 1. Input validation — mirrors TS purge.ts:124-141.
    if not opts.reason or not opts.reason.strip():
        raise PurgeError("missing_reason", "[purge] reason is required")

    crit = opts.criteria
    has_criteria = bool(
        (crit.summary_ids and len(crit.summary_ids) > 0)
        or crit.session_key
        or crit.since
        or crit.before
        or crit.min_token_count is not None
    )
    if not has_criteria:
        raise PurgeError(
            "no_criteria",
            "[purge] at least one criterion required (summary_ids, "
            "session_key, since/before, or min_token_count)",
        )
    if crit.session_key == "agent:main:main" and not opts.allow_main_session:
        raise PurgeError(
            "main_session_blocked",
            "[purge] refusing to purge agent:main:main without allow_main_session=True",
        )

    purge_session_id = f"purge_{int(time.time() * 1000)}_{secrets.token_hex(3)}"

    # 2. Atomic cascade — resolve + 6 updates in one BEGIN IMMEDIATE.
    # LCM Wave-8 (2026-04-15): Auditor #13-18 E-P1 fix — resolve
    # targetLeaves INSIDE the BEGIN IMMEDIATE transaction so a concurrent
    # /lcm purge or suppression update can't change the leaf set between
    # resolve and UPDATE. Previously resolve ran outside the tx → audit-
    # trail loss when an already-suppressed leaf got re-stamped with a
    # new reason.
    # Original: lossless-claw/src/operator/purge.ts:144-152.
    return _run_soft_purge_atomic(db, opts, purge_session_id)


# ---------------------------------------------------------------------------
# Internals — resolver + cascade body
# ---------------------------------------------------------------------------


def _resolve_target_leaf_ids(
    db: sqlite3.Connection,
    crit: PurgeCriteria,
) -> list[str]:
    """Resolve a :class:`PurgeCriteria` to the actual list of leaf IDs.

    Ports the TS ``resolveTargetLeafIds`` (purge.ts:202-242). Two
    branches:

    * **summary_ids branch** — when ``crit.summary_ids`` is non-empty,
      validate each ID exists AND is ``kind='leaf'`` AND is not yet
      suppressed. Operator mistakes (typos, non-existent IDs, condensed
      IDs, already-suppressed IDs) silently filter out — by design, the
      operator's "purge these 5 IDs" intent shouldn't half-execute if
      one ID is a typo.
    * **range branch** — combines ``session_key``, ``since``, ``before``,
      and ``min_token_count`` with AND. Always filters
      ``kind='leaf' AND suppressed_at IS NULL`` (LCM Wave-9 TS-tightening
      preserved: only resolve non-suppressed leaves so re-running purge
      is idempotent).

    Args:
        db: Open SQLite connection. Read-only; the function does NOT
            modify the DB and does NOT open a transaction.
        crit: The scope criteria. ``crit.summary_ids`` takes priority
            when non-empty.

    Returns:
        List of ``summary_id`` strings, in DB-return order (SQLite
        leaves order undefined; callers should treat as a set).
    """
    if crit.summary_ids and len(crit.summary_ids) > 0:
        placeholders = ",".join(["?"] * len(crit.summary_ids))
        rows = db.execute(
            f"""
            SELECT summary_id FROM summaries
              WHERE summary_id IN ({placeholders})
                AND kind = 'leaf'
                AND suppressed_at IS NULL
            """,
            tuple(crit.summary_ids),
        ).fetchall()
        return [row[0] for row in rows]

    # Range purge — combine optional criteria with AND.
    conds: list[str] = ["kind = 'leaf'", "suppressed_at IS NULL"]
    args: list[str | int] = []
    if crit.session_key:
        conds.append("session_key = ?")
        args.append(crit.session_key)
    if crit.since:
        conds.append("created_at >= ?")
        args.append(_iso(crit.since))
    if crit.before:
        conds.append("created_at < ?")
        args.append(_iso(crit.before))
    if crit.min_token_count is not None:
        conds.append("token_count >= ?")
        args.append(crit.min_token_count)
    sql = f"SELECT summary_id FROM summaries WHERE {' AND '.join(conds)}"
    rows = db.execute(sql, tuple(args)).fetchall()
    return [row[0] for row in rows]


def _run_soft_purge_atomic(
    db: sqlite3.Connection,
    opts: PurgeOptions,
    purge_session_id: str,
) -> PurgeResult:
    """Open ``BEGIN IMMEDIATE``, resolve, run the 6 cascade steps, COMMIT.

    Ports the TS ``runSoftPurgeAtomic`` (purge.ts:155-180). The resolve
    + cascade share one transaction (Wave-8 Auditor #13-18 E-P1 fix
    preserved — see ``run_purge`` docstring). On any error, ``ROLLBACK``
    is invoked before the exception propagates so no partial state
    survives.

    Empty-match path: when the resolver returns ``[]``, we ``COMMIT`` an
    empty transaction (no cascade ran) and return an empty result. This
    is NOT an error — operators re-running a purge against an already-
    purged set get a clean empty result, not a crash.

    Args:
        db: Open SQLite connection in autocommit mode.
        opts: Full purge input.
        purge_session_id: The in-memory traceability token to return.

    Returns:
        :class:`PurgeResult` — see :func:`run_purge`.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        target_leaves = _resolve_target_leaf_ids(db, opts.criteria)
        if len(target_leaves) == 0:
            db.execute("COMMIT")
            return PurgeResult(
                affected_leaf_ids=[],
                purge_session_id=purge_session_id,
                mode="soft",
            )
        result = _run_soft_purge_body(
            db,
            leaf_ids=target_leaves,
            reason=opts.reason,
            purge_session_id=purge_session_id,
            already_in_tx=True,
        )
        return result
    except Exception:
        # LCM Wave-8 (2026-04-15): swallow ROLLBACK errors — if the
        # outer exception is "cannot rollback (no transaction)" we
        # don't want to mask the original error. Best-effort rollback.
        # Original: lossless-claw/src/operator/purge.ts:177.
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _run_soft_purge_body(
    db: sqlite3.Connection,
    *,
    leaf_ids: list[str],
    reason: str,
    purge_session_id: str,
    already_in_tx: bool,
) -> PurgeResult:
    """Execute the 6-step soft-suppression cascade.

    Ports the TS ``runSoftPurgeBody`` (purge.ts:244-369). Caller MUST
    have already opened ``BEGIN IMMEDIATE`` (``already_in_tx=True``) —
    we don't open one here to avoid nested-tx errors (SQLite would error
    "cannot start a transaction within a transaction").

    The 6 steps fire in strict order:

    1. ``UPDATE summaries SET suppressed_at, suppress_reason`` for the
       leaf IDs (also fires the per-model vec0 trigger if vec0 is
       loaded — Epic 05 wiring).
    2. ``UPDATE summaries SET contains_suppressed_leaves = 1`` for
       condensed summaries whose parent links include any of the leaves.
    3. ``DELETE FROM context_items WHERE item_type='summary'`` for the
       leaf IDs (cuts assembler pointer for summary-type items).
    4. ``DELETE FROM context_items WHERE item_type='message'`` for any
       messages linked via ``summary_messages`` to the leaves (cuts
       assembler pointer for message-type items).
    5. ``UPDATE messages SET suppressed_at`` for messages linked via
       ``summary_messages`` — Wave-7 P0-2 fix: GATED by ``NOT EXISTS``
       on a non-suppressed referencing summary outside the purge set,
       so a message shared with a non-purged leaf is not orphaned.
    6. ``DELETE FROM lcm_synthesis_cache`` for caches that referenced
       the leaves via ``lcm_cache_leaf_refs``.

    On error, the caller (:func:`_run_soft_purge_atomic`) catches and
    ROLLBACKs.

    Args:
        db: Open SQLite connection.
        leaf_ids: The resolved target leaf IDs (non-empty).
        reason: Audit-trail reason (validated non-empty by
            :func:`run_purge`).
        purge_session_id: Traceability token.
        already_in_tx: Must be ``True``; argument exists for
            byte-equivalence with the TS source's ``alreadyInTx``
            parameter and to make the "we don't open our own tx"
            invariant explicit.

    Returns:
        :class:`PurgeResult` with the input ``leaf_ids`` as
        ``affected_leaf_ids``.
    """
    # Defensive: keep the signature symmetric with TS even though
    # only one call path (_run_soft_purge_atomic) exists.
    if not already_in_tx:
        # Reserved for a future un-wrapped caller. Currently every code
        # path goes through _run_soft_purge_atomic which already holds
        # BEGIN IMMEDIATE. If we ever expose a public soft_purge() that
        # takes a pre-resolved leaf list, this branch would open the tx.
        db.execute("BEGIN IMMEDIATE")

    try:
        placeholders = ",".join(["?"] * len(leaf_ids))

        # --- Step 1: suppress the leaf summaries -----------------
        # Fires per-model vec0 trigger lcm_embed_suppress_<slug> when
        # vec0 is loaded (Epic 05). Without vec0, this is just a
        # straight UPDATE.
        db.execute(
            f"""
            UPDATE summaries
              SET suppressed_at = datetime('now'),
                  suppress_reason = ?
              WHERE summary_id IN ({placeholders})
            """,
            (reason, *leaf_ids),
        )

        # --- Step 2: flag condensed summaries containing these leaves --
        # summary_parents schema: (summary_id = condensed,
        # parent_summary_id = leaf). The idle-rebuild marker drives
        # Epic 04 compaction to re-summarize the condensed without
        # the now-suppressed content.
        db.execute(
            f"""
            UPDATE summaries
              SET contains_suppressed_leaves = 1
              WHERE kind = 'condensed' AND summary_id IN (
                SELECT DISTINCT summary_id FROM summary_parents
                  WHERE parent_summary_id IN ({placeholders})
              )
            """,
            tuple(leaf_ids),
        )

        # --- Step 3: cut assembler pointer (summary-type items) ----
        # v4.1 §10 + Final review #1 fix preserved from TS. Without
        # this, the assembler's resolveSummaryItem would still try to
        # resolve the suppressed summary by ID via context_items.summary_id.
        # The summary-store's default suppressed_at filter would skip it,
        # but cleaning context_items here is the cleanest cut.
        # Original: lossless-claw/src/operator/purge.ts:281-284.
        db.execute(
            f"""
            DELETE FROM context_items
              WHERE item_type = 'summary' AND summary_id IN ({placeholders})
            """,
            tuple(leaf_ids),
        )

        # --- Step 4: cut assembler pointer (message-type items) ----
        # v4.1 Final.review.3 Loop 2 Leak 2.1 fix preserved from TS.
        # Without this, the assembler's resolveMessageItem →
        # conversationStore.getMessageById still loaded the suppressed
        # message content because the context_items pointer survived.
        # The conversationStore default suppressed_at filter handles the
        # read-side; this delete is defense-in-depth.
        # Original: lossless-claw/src/operator/purge.ts:295-301.
        db.execute(
            f"""
            DELETE FROM context_items
              WHERE item_type = 'message' AND message_id IN (
                SELECT message_id FROM summary_messages
                  WHERE summary_id IN ({placeholders})
              )
            """,
            tuple(leaf_ids),
        )

        # --- Step 5: cascade suppression to raw messages -------------
        # LCM Wave-7 (2026-02-14): Auditor #14 P0-2 fix — only suppress
        # messages whose EVERY referencing leaf is being suppressed.
        # Without this gate, purging one of two leaves that share a
        # message would silently suppress the message for both — breaking
        # the non-purged leaf's assemble path. The NOT EXISTS predicate
        # checks for any non-suppressed referencing summary OUTSIDE the
        # current purge set.
        # Original: lossless-claw/src/operator/purge.ts:323-336.
        db.execute(
            f"""
            UPDATE messages SET suppressed_at = datetime('now')
              WHERE message_id IN (
                SELECT sm.message_id FROM summary_messages sm
                  WHERE sm.summary_id IN ({placeholders})
              )
              AND NOT EXISTS (
                SELECT 1 FROM summary_messages sm2
                  JOIN summaries s2 ON s2.summary_id = sm2.summary_id
                  WHERE sm2.message_id = messages.message_id
                    AND s2.suppressed_at IS NULL
                    AND sm2.summary_id NOT IN ({placeholders})
              )
            """,
            (*leaf_ids, *leaf_ids),
        )

        # --- Step 6: invalidate dependent synthesis caches -----------
        # v4.1 Final.review.3 Loop 2 Leak 2.5 fix preserved from TS.
        # lcm_cache_leaf_refs has ON DELETE CASCADE on both
        # lcm_synthesis_cache.cache_id and summaries.summary_id, but the
        # cascade only fires on hard DELETE, not on soft suppression.
        # We MUST DELETE the cache rows explicitly so any future cache
        # read (or future re-synthesis) doesn't surface PII baked in
        # before suppression. Cache is REBUILDABLE by design — losing
        # rows is safe.
        # Original: lossless-claw/src/operator/purge.ts:346-352.
        db.execute(
            f"""
            DELETE FROM lcm_synthesis_cache
              WHERE cache_id IN (
                SELECT DISTINCT cache_id FROM lcm_cache_leaf_refs
                  WHERE leaf_summary_id IN ({placeholders})
              )
            """,
            tuple(leaf_ids),
        )

        if not already_in_tx:
            db.execute("COMMIT")
        else:
            # Caller (_run_soft_purge_atomic) commits after we return.
            # Inline commit here to match TS atomic-fn semantics —
            # _run_soft_purge_atomic relies on this body to commit so
            # both `already_in_tx=True` and `False` paths produce the
            # same "by the time this function returns, the tx is closed"
            # semantics.
            db.execute("COMMIT")
    except Exception:
        # LCM Wave-8 (2026-04-15): only ROLLBACK if WE opened the tx;
        # otherwise let outer try/catch handle it. Currently the only
        # caller is _run_soft_purge_atomic which also ROLLBACKs, so we
        # always have the outer guard — but the symmetry matches TS.
        # Original: lossless-claw/src/operator/purge.ts:356-360.
        if not already_in_tx:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise

    return PurgeResult(
        affected_leaf_ids=leaf_ids,
        purge_session_id=purge_session_id,
        mode="soft",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    """Convert a :class:`datetime` to the ISO-8601 string SQLite expects.

    Mirrors the TS ``date.toISOString()`` behavior (purge.ts:229, 233).
    Naive datetimes are formatted as-is (caller's responsibility to pass
    UTC); timezone-aware datetimes are normalized to UTC via
    :py:meth:`datetime.isoformat`.

    SQLite compares ``created_at`` as ISO-8601 strings lexically, so
    micro-second precision matters less than the format being consistent.
    """
    return dt.isoformat()


__all__ = [
    "PurgeCriteria",
    "PurgeError",
    "PurgeErrorKind",
    "PurgeOptions",
    "PurgeResult",
    "preview_purge_affected",
    "run_purge",
]
