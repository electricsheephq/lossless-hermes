"""Soft-suppression prune cascade for LCM session content.

Ports the 6-step ``runPurge``/``runSoftPurgeBody`` cascade from
``lossless-claw/src/operator/purge.ts`` (commit ``1f07fbd``, ~390 LOC) to
Python, *not* the hard-delete ``pruneConversations`` from ``src/prune.ts``.

Per the issue spec (task brief + doctor-ops.md §"Prune cascade"):

* This module owns the **soft-suppression** path — flips ``suppressed_at``
  + cascades through ``summaries`` / ``messages`` / ``context_items`` /
  ``lcm_synthesis_cache`` so no agent-visible read can resurface the
  purged content.
* The hard-delete ``pruneConversations`` path (data-retention age-based
  delete) ports separately in a later issue when the operator surfaces
  land; that surface is **deferred** per ``doctor-ops.md`` §"Prune cascade"
  (immediate-mode hard-delete drainer was removed from LCM upstream in the
  first-principles pass).

### The 6-step BEGIN IMMEDIATE cascade

All six steps run inside a single ``BEGIN IMMEDIATE`` transaction so a
crash mid-cascade either leaves nothing applied or everything applied.
The order matters — context_items deletion must happen *before* the
``messages.suppressed_at`` cascade, because the messages cascade's
``NOT EXISTS`` predicate inspects the unfiltered ``summary_messages`` set.

1. **``UPDATE summaries SET suppressed_at = datetime('now'),
   suppress_reason = ?``** for matched leaf summary IDs. Per
   ``doctor-ops.md`` this UPDATE fires the per-model
   ``lcm_embed_suppress_<slug>`` triggers (in v4.1, once the embeddings
   epic lands) so vec0 metadata flips the ``suppressed`` column —
   semantic search filters them automatically.
2. **``UPDATE summaries SET contains_suppressed_leaves = 1``** for
   condensed summaries whose ``summary_parents.parent_summary_id`` is one
   of the suppressed leaves. Flags them for idle rebuild.
3. **``DELETE FROM context_items WHERE item_type='summary' AND
   summary_id IN (...)``** — removes the assembler's pointer so the
   suppressed summary cannot be re-emitted into the prompt.
4. **``DELETE FROM context_items WHERE item_type='message' AND
   message_id IN (SELECT message_id FROM summary_messages WHERE summary_id
   IN (...))``** — cuts the message-level pointer for the same reason.
5. **``UPDATE messages SET suppressed_at = datetime('now')``** for
   messages linked via ``summary_messages`` to suppressed leaves —
   **gated by** ``NOT EXISTS`` on any non-suppressed referencing summary
   outside the purge set, so a message shared with a non-purged leaf is
   not orphaned (Wave-7 Auditor #14 P0-2 fix).
6. **``DELETE FROM lcm_synthesis_cache WHERE cache_id IN (SELECT DISTINCT
   cache_id FROM lcm_cache_leaf_refs WHERE leaf_summary_id IN (...))``**
   — invalidates rebuildable synthesis caches that referenced the
   suppressed leaves. The cache schema's ``ON DELETE CASCADE`` only fires
   on hard DELETE; soft suppression must do this explicitly (v4.1
   Final.review.3 fix Loop 2 Leak 2.5).

### Wave-N provenance (ADR-029)

Multiple steps in this cascade are Wave-N fixes from LCM's audit history:

* Step 5 (messages cascade with ``NOT EXISTS`` gate) — **Wave-7 Auditor
  #14 P0-2**: without the gate, purging one of two leaves that share a
  message silently suppresses the message for both, breaking the non-
  purged leaf's assemble path.
* Steps 3/4 (context_items deletion) — v4.1 Final.review fixes: leaving
  the rows in place lets the assembler's hot path (``resolveSummaryItem``,
  ``resolveMessageItem``) silently load suppressed content back into the
  prompt; ``suppressed_at IS NULL`` filtering at read time is a
  defense-in-depth backstop, not the primary cut.
* Step 6 (synthesis-cache invalidation) — v4.1 Final.review.3 Loop 2
  Leak 2.5: the synthesis cache survives soft suppression, so a future
  cache read or rebuild can surface PII baked in before the purge.
* Atomic resolve+update (the ``BEGIN IMMEDIATE``-scoped target resolution)
  — **Wave-8 Auditor #13-18 E-P1**: resolving target leaves *outside*
  the transaction lost the audit trail when a concurrent purge re-stamped
  an already-suppressed leaf.

### Public surface

* :func:`soft_prune_session` — convenience entry keyed by ``session_key``.
  Resolves every active (un-suppressed) leaf summary under the session
  and runs the 6-step cascade. Returns :class:`PruneResult` with the
  affected summary id set.
* :func:`soft_prune_summary_ids` — explicit-list variant. Resolves the
  supplied ids, filters to ``kind='leaf'`` and ``suppressed_at IS NULL``,
  then runs the 6-step cascade. Matches TS' ``runPurge(opts.summaryIds=…)``.
* :func:`preview_soft_prune_affected` — read-only count of leaves that
  would be affected. Mirrors TS ``previewPurgeAffected`` (Wave-2 Auditor
  #6 BUG-2/3 — predicate parity with apply so dry-run == apply count).

### Schema dependencies

The cascade reads/writes these tables (all created by ``run_lcm_migrations``
in issue #01-04 except where noted):

* ``summaries.suppressed_at`` / ``suppress_reason`` /
  ``contains_suppressed_leaves`` — structural columns probed by
  :func:`_apply_structural_column_probes` in #01-04.
* ``messages.suppressed_at`` — same probe.
* ``summary_parents`` — base FK-target table.
* ``summary_messages`` — base FK-target table.
* ``context_items`` — base table.
* ``lcm_synthesis_cache`` / ``lcm_cache_leaf_refs`` — **v4.1 tables**
  created by :func:`_ensure_v41_tables` in **#01-06** (deferred). The
  cascade step 6 is gated by :func:`_has_table` so this module is
  safely callable on a #01-04-only DB.

See:

* ``docs/porting-guides/doctor-ops.md`` §"Prune cascade — soft-suppression
  + hard-delete behaviors" — the canonical cascade spec.
* ADR-018 — ``ConversationLockManager`` for async serialization.
* ADR-029 — Wave-N fix provenance.
* ``lossless-claw/src/operator/purge.ts`` — verbatim TS source.
* ``epics/01-storage/01-13-integrity-prune.md`` — issue spec.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

__all__ = [
    "PruneCriteria",
    "PruneError",
    "PruneResult",
    "preview_soft_prune_affected",
    "soft_prune_session",
    "soft_prune_summary_ids",
]

_log = logging.getLogger("lossless_hermes.prune")


# ---------------------------------------------------------------------------
# Errors + result types
# ---------------------------------------------------------------------------


class PruneError(Exception):
    """Operator-validation error raised by :func:`soft_prune_*` entry points.

    Mirrors TS ``PurgeError`` shape: a ``kind`` discriminator + a message.
    """

    def __init__(
        self,
        kind: str,
        message: str,
    ) -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PruneCriteria:
    """Range-style purge criteria. Mirrors TS ``PurgeCriteria``.

    All fields are optional but at least one must be set (enforced by
    :func:`_validate_criteria`). Combine fields with implicit ``AND``.

    Attributes:
        summary_ids: explicit list of summary ids to suppress. Highest
            precedence — when set, the range fields are ignored.
        session_key: restrict to leaves under this session key.
        since: include only leaves with ``created_at >= since``.
        before: include only leaves with ``created_at < before``.
        min_token_count: include only leaves with ``token_count >= min``.
    """

    summary_ids: tuple[str, ...] | None = None
    session_key: str | None = None
    since: datetime | None = None
    before: datetime | None = None
    min_token_count: int | None = None


@dataclass(frozen=True, slots=True)
class PruneResult:
    """Outcome of a soft-prune pass.

    Mirrors TS ``PurgeResult``: affected leaf ids + an opaque session id
    for downstream audit logging. ``mode='soft'`` is always the case here
    because the hard-delete drainer was removed from LCM upstream and is
    not in scope for this port.
    """

    affected_leaf_ids: tuple[str, ...]
    prune_session_id: str
    mode: str = "soft"
    # Per-step row counts, useful for tests + observability. Step 1 is the
    # summary suppression; step 5 is the message cascade; step 6 is the
    # synthesis-cache invalidation. Other steps are derived counts.
    counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_table(conn: sqlite3.Connection, table_name: str) -> bool:
    """Return ``True`` if a user table with ``table_name`` exists.

    Used to gate the synthesis-cache cascade (step 6) so this module
    works on a #01-04-only DB. The v4.1 cache tables come with #01-06.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _validate_reason(reason: str) -> None:
    """Reject empty / whitespace-only reasons.

    Mirrors TS check in ``runPurge`` (lines 124-126). The reason is
    recorded in ``summaries.suppress_reason`` and is load-bearing for
    audit — empty values would silently strip context.
    """
    if not reason or not reason.strip():
        raise PruneError("missing_reason", "[soft-prune] reason is required")


def _validate_criteria(criteria: PruneCriteria) -> None:
    """Reject criteria with no selectors.

    Mirrors the TS ``hasCriteria`` gate. Without at least one selector
    field, the resolve query would match every leaf summary in the DB —
    the operator equivalent of ``rm -rf``.
    """
    if criteria.summary_ids and len(criteria.summary_ids) > 0:
        return
    has_range = any((
        criteria.session_key,
        criteria.since,
        criteria.before,
        criteria.min_token_count is not None,
    ))
    if not has_range:
        raise PruneError(
            "no_criteria",
            "[soft-prune] at least one criterion required "
            "(summary_ids, session_key, since/before, or min_token_count)",
        )


def _generate_prune_session_id() -> str:
    """Return an opaque ``purge_<ts>_<rand>`` id for audit logs.

    Mirrors TS ``runPurge`` (line 143): ``purge_${Date.now()}_${rand}``.
    ``secrets.token_hex(3)`` gives a 6-hex-char suffix (24 bits — collision
    probability ~1e-7 across a million calls in one millisecond, which is
    well beyond any realistic operator workload).
    """
    ts_ms = int(time.time() * 1000)
    return f"prune_{ts_ms}_{secrets.token_hex(3)}"


def _resolve_target_leaf_ids(conn: sqlite3.Connection, criteria: PruneCriteria) -> list[str]:
    """Resolve criteria to a list of leaf summary ids.

    Mirrors TS ``resolveTargetLeafIds`` (lines 202-242). The query
    structure preserves TS predicate ordering so the dry-run count
    matches the apply count exactly (Wave-2 Auditor #6 BUG-2/3 fix).

    Returns leaf ids only (``kind='leaf'``) that are not already
    suppressed (``suppressed_at IS NULL``).
    """
    # Explicit summary ids — validate each exists + is a leaf + un-suppressed.
    if criteria.summary_ids:
        placeholders = ",".join("?" * len(criteria.summary_ids))
        rows = conn.execute(
            f"""
            SELECT summary_id FROM summaries
             WHERE summary_id IN ({placeholders})
               AND kind = 'leaf'
               AND suppressed_at IS NULL
            """,
            tuple(criteria.summary_ids),
        ).fetchall()
        return [r[0] for r in rows]

    # Range query — combine `WHERE` clauses with explicit args order so the
    # parameter binding stays positional.
    conditions: list[str] = ["kind = 'leaf'", "suppressed_at IS NULL"]
    args: list[object] = []
    if criteria.session_key is not None:
        conditions.append(
            "conversation_id IN (SELECT conversation_id FROM conversations WHERE session_key = ?)"
        )
        args.append(criteria.session_key)
    if criteria.since is not None:
        conditions.append("created_at >= ?")
        args.append(criteria.since.isoformat())
    if criteria.before is not None:
        conditions.append("created_at < ?")
        args.append(criteria.before.isoformat())
    if criteria.min_token_count is not None:
        conditions.append("token_count >= ?")
        args.append(criteria.min_token_count)

    sql = f"SELECT summary_id FROM summaries WHERE {' AND '.join(conditions)}"
    rows = conn.execute(sql, tuple(args)).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Public surface — preview
# ---------------------------------------------------------------------------


def preview_soft_prune_affected(conn: sqlite3.Connection, criteria: PruneCriteria) -> int:
    """Count leaves that :func:`soft_prune_session` would suppress.

    Read-only. Uses the **same** predicate as :func:`_resolve_target_leaf_ids`
    so the dry-run count matches the apply count exactly (Wave-2 Auditor
    #6 BUG-2/3 — predicate parity).

    Does not validate ``reason`` (which is required for apply) — callers
    typically render the preview before prompting the operator for a
    confirmation + reason.

    Mirrors TS ``previewPurgeAffected``.
    """
    _validate_criteria(criteria)
    return len(_resolve_target_leaf_ids(conn, criteria))


# ---------------------------------------------------------------------------
# Public surface — apply (the 6-step cascade)
# ---------------------------------------------------------------------------


def soft_prune_summary_ids(
    conn: sqlite3.Connection,
    summary_ids: Sequence[str],
    *,
    reason: str,
) -> PruneResult:
    """Soft-suppress an explicit list of leaf summaries.

    Resolves the supplied ids (filtered to ``kind='leaf'`` and
    ``suppressed_at IS NULL``), then runs the 6-step cascade inside a
    single ``BEGIN IMMEDIATE`` transaction.

    Args:
        conn: open sqlite3 connection. Per ADR-017 the cascade is
            synchronous; if you need async serialization across tasks,
            wrap the call in :class:`ConversationLockManager.transaction`
            keyed by the session id.
        summary_ids: explicit leaf ids to suppress. Non-existent ids,
            non-leaf ids, and already-suppressed ids are silently dropped
            (mirrors TS shape — the operator sees the *actual* affected
            set in the result).
        reason: required free-text reason recorded in
            ``summaries.suppress_reason``. Empty / whitespace-only values
            raise :class:`PruneError`.

    Returns:
        :class:`PruneResult` with the affected ids and a fresh session id.
    """
    _validate_reason(reason)
    criteria = PruneCriteria(summary_ids=tuple(summary_ids))
    _validate_criteria(criteria)
    return _run_soft_prune_atomic(conn, criteria, reason)


def soft_prune_session(
    conn: sqlite3.Connection,
    session_key: str,
    *,
    reason: str,
    since: datetime | None = None,
    before: datetime | None = None,
    min_token_count: int | None = None,
    allow_main_session: bool = False,
) -> PruneResult:
    """Soft-suppress every active leaf summary under ``session_key``.

    Convenience entry point matching the task brief signature. Resolves
    leaves via :class:`PruneCriteria`(session_key=..., ...) and runs the
    6-step cascade.

    Args:
        conn: open sqlite3 connection.
        session_key: the session_key to scope the purge to. Must be set.
        reason: required free-text reason.
        since: optional ``created_at >= since`` filter.
        before: optional ``created_at < before`` filter.
        min_token_count: optional ``token_count >= min`` filter.
        allow_main_session: defense against accidental ``agent:main:main``
            wipes; mirrors TS ``allowMainSession`` flag.

    Returns:
        :class:`PruneResult` with the affected ids and a fresh session id.

    Raises:
        PruneError: missing/empty reason, missing/empty session_key, or
            ``agent:main:main`` without ``allow_main_session=True``.
    """
    _validate_reason(reason)
    if not session_key or not session_key.strip():
        raise PruneError(
            "no_criteria",
            "[soft-prune] session_key is required for soft_prune_session",
        )
    if session_key == "agent:main:main" and not allow_main_session:
        raise PruneError(
            "main_session_blocked",
            "[soft-prune] refusing to purge agent:main:main without allow_main_session=True",
        )
    criteria = PruneCriteria(
        session_key=session_key,
        since=since,
        before=before,
        min_token_count=min_token_count,
    )
    _validate_criteria(criteria)
    return _run_soft_prune_atomic(conn, criteria, reason)


# ---------------------------------------------------------------------------
# Internals — the atomic resolve+cascade
# ---------------------------------------------------------------------------


def _run_soft_prune_atomic(
    conn: sqlite3.Connection,
    criteria: PruneCriteria,
    reason: str,
) -> PruneResult:
    """Resolve targets and run the 6-step cascade inside one BEGIN IMMEDIATE.

    Mirrors TS ``runSoftPurgeAtomic`` (lines 155-180): the target resolution
    *and* the cascade UPDATEs share a single transaction so a concurrent
    purge or suppression update cannot change the leaf set between resolve
    and update.

    LCM Wave-8 (2025-12-02): Auditor #13-18 E-P1 fix — resolving outside
    the tx caused an audit-trail loss when a concurrent /lcm purge re-
    stamped an already-suppressed leaf with a new reason. By doing
    resolve + updates atomically, we guarantee the affected set is
    consistent with what was actually written.
    """
    prune_session_id = _generate_prune_session_id()

    # Flush any implicit transaction Python's sqlite3 module may have opened
    # on prior DML. With ``isolation_level=""`` (the stdlib default), Python
    # auto-issues a BEGIN before the first INSERT/UPDATE/DELETE; our explicit
    # ``BEGIN IMMEDIATE`` here would otherwise raise "cannot start a
    # transaction within a transaction". ``commit()`` when no implicit txn
    # is open is safe and cheap.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        target_leaves = _resolve_target_leaf_ids(conn, criteria)
        if not target_leaves:
            conn.execute("COMMIT")
            return PruneResult(
                affected_leaf_ids=(),
                prune_session_id=prune_session_id,
                mode="soft",
                counts={
                    "summaries_suppressed": 0,
                    "condensed_flagged": 0,
                    "context_items_summary_deleted": 0,
                    "context_items_message_deleted": 0,
                    "messages_suppressed": 0,
                    "synthesis_cache_invalidated": 0,
                },
            )
        counts = _run_soft_prune_body(conn, target_leaves, reason)
        conn.execute("COMMIT")
    except BaseException:
        # Defensive: ROLLBACK on any exit path that isn't a clean COMMIT.
        # ``try ... except`` for ``sqlite3.OperationalError`` swallowed
        # because we want the original exception to propagate.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:  # pragma: no cover - rare double-fault
            _log.warning(
                "ROLLBACK failed during soft-prune session %s",
                prune_session_id,
                exc_info=True,
            )
        raise

    return PruneResult(
        affected_leaf_ids=tuple(target_leaves),
        prune_session_id=prune_session_id,
        mode="soft",
        counts=counts,
    )


def _run_soft_prune_body(
    conn: sqlite3.Connection,
    leaf_ids: list[str],
    reason: str,
) -> dict[str, int]:
    """Execute the 6-step cascade against ``leaf_ids``.

    Caller must already hold an open ``BEGIN IMMEDIATE`` transaction
    (mirrors TS ``runSoftPurgeBody`` with ``alreadyInTx=True``). Returns
    per-step row counts for observability.
    """
    placeholders = ",".join("?" * len(leaf_ids))
    leaf_ids_tuple = tuple(leaf_ids)
    counts: dict[str, int] = {}

    # ---- Step 1: suppress the leaf summaries ----
    # LCM v4.1 §10 / Final.review #1: flips ``suppressed_at`` + records the
    # operator's reason. Fires the per-model lcm_embed_suppress_<slug>
    # trigger (created by the embeddings store in #01-06+; safe no-op until
    # then) so the vec0 ``suppressed`` metadata column tracks the suppression
    # automatically — semantic search filters them.
    cur = conn.execute(
        f"""
        UPDATE summaries
           SET suppressed_at = datetime('now'),
               suppress_reason = ?
         WHERE summary_id IN ({placeholders})
        """,
        (reason, *leaf_ids_tuple),
    )
    counts["summaries_suppressed"] = cur.rowcount

    # ---- Step 2: flag condensed summaries containing suppressed leaves ----
    # The summary_parents schema is (summary_id = condensed, parent_summary_id
    # = leaf). Any condensed whose parent set includes a suppressed leaf is
    # marked for idle rebuild via ``contains_suppressed_leaves = 1``.
    cur = conn.execute(
        f"""
        UPDATE summaries
           SET contains_suppressed_leaves = 1
         WHERE kind = 'condensed'
           AND summary_id IN (
             SELECT DISTINCT summary_id FROM summary_parents
              WHERE parent_summary_id IN ({placeholders})
           )
        """,
        leaf_ids_tuple,
    )
    counts["condensed_flagged"] = cur.rowcount

    # ---- Step 3: cut context_items pointers to the suppressed summaries ----
    # LCM v4.1 §10 + Final.review #1: the assembler hot path resolves
    # summaries by id via context_items.summary_id. If we leave the pointer
    # in place, even a suppressed summary could be re-emitted into the
    # prompt unless every read site checks ``suppressed_at IS NULL``.
    # Removing the rows here is the cleanest cut.
    cur = conn.execute(
        f"""
        DELETE FROM context_items
         WHERE item_type = 'summary'
           AND summary_id IN ({placeholders})
        """,
        leaf_ids_tuple,
    )
    counts["context_items_summary_deleted"] = cur.rowcount

    # ---- Step 4: cut context_items pointers to the underlying messages ----
    # LCM Final.review.3 (Loop 2 Leak 2.1 BLOCKER companion): without this,
    # the assembler hot path (resolveMessageItem → getMessageById) still
    # loaded suppressed message content into the prompt because the
    # context_items pointer survived. The getMessageById path filters
    # suppressed_at IS NULL by default; this step is the upstream cut.
    cur = conn.execute(
        f"""
        DELETE FROM context_items
         WHERE item_type = 'message'
           AND message_id IN (
             SELECT message_id FROM summary_messages
              WHERE summary_id IN ({placeholders})
           )
        """,
        leaf_ids_tuple,
    )
    counts["context_items_message_deleted"] = cur.rowcount

    # ---- Step 5: cascade suppression to the underlying raw messages ----
    # LCM v4.1 Final.review P1 #2: without this, lcm_grep mode='regex' /
    # 'full_text' scope='messages' or 'both' would still find purged
    # content via the raw messages table (which has its own FTS index).
    #
    # LCM Wave-7 (2025-11-19): Auditor #14 P0-2 fix — only suppress messages
    # whose EVERY referencing leaf is being suppressed. Without the
    # NOT EXISTS gate, purging one of two leaves that share a message
    # silently suppresses the message for both, breaking the non-purged
    # leaf's assemble path. The predicate checks for any non-suppressed
    # referencing summary OUTSIDE the current purge set.
    cur = conn.execute(
        f"""
        UPDATE messages
           SET suppressed_at = datetime('now')
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
        (*leaf_ids_tuple, *leaf_ids_tuple),
    )
    counts["messages_suppressed"] = cur.rowcount

    # ---- Step 6: invalidate rebuildable synthesis caches ----
    # LCM Final.review.3 (Loop 2 Leak 2.5): lcm_cache_leaf_refs has
    # ``ON DELETE CASCADE`` on both ``lcm_synthesis_cache.cache_id`` and
    # ``summaries.summary_id``, but the cascade only fires on hard DELETE,
    # not on soft suppression. We MUST DELETE the cache rows explicitly so
    # any future cache read (or future re-synthesis) doesn't surface PII
    # baked in before suppression. Cache is REBUILDABLE by design — losing
    # rows is safe.
    #
    # Gated by ``_has_table`` because the v4.1 cache tables come with
    # #01-06; on a #01-04-only DB this step is a no-op (count=0).
    if _has_table(conn, "lcm_synthesis_cache") and _has_table(conn, "lcm_cache_leaf_refs"):
        cur = conn.execute(
            f"""
            DELETE FROM lcm_synthesis_cache
             WHERE cache_id IN (
                 SELECT DISTINCT cache_id FROM lcm_cache_leaf_refs
                  WHERE leaf_summary_id IN ({placeholders})
             )
            """,
            leaf_ids_tuple,
        )
        counts["synthesis_cache_invalidated"] = cur.rowcount
    else:
        counts["synthesis_cache_invalidated"] = 0

    return counts
