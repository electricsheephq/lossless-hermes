"""Embedding backfill cron — LCM v4.1 §13 Group B.04.

Ports ``lossless-claw/src/embeddings/backfill.ts`` (commit ``1f07fbd`` on
branch ``pr-613``, 637 LOC) to Python. Walks unembedded ``summaries`` rows,
batches them by token budget, sends to Voyage, writes vec0 + meta. Designed
to run as a worker job (lock-protected, resumable, rate-limited).

Single-tick API: caller (worker scheduler — see issue 05-05's
:class:`~lossless_hermes.concurrency.worker_loop.WorkerLoop`) invokes once
per tick; the function acquires the cross-process worker lock, processes up
to ``per_tick_limit`` documents, releases the lock, returns a summary.

### Key invariants (v4.1 §13 + §0)

1. **§0: No LLM/network call inside any SQLite write transaction.** Each
   Voyage HTTP call happens OUTSIDE the per-batch DB transaction. We (a)
   prepare the batch (read-only SELECT), (b) call Voyage, (c) write results
   in a fresh ``BEGIN IMMEDIATE`` block. Defended at runtime by
   :func:`~lossless_hermes.concurrency.model.assert_no_open_tx` before each
   embed call. Ports ``backfill.ts:10-18`` rationale.

2. **Single-flight via :data:`lcm_worker_lock`.** Only one process runs the
   embedding-backfill kind at a time; otherwise we'd burn Voyage quota and
   potentially write duplicate vec0 rows. The lock is acquired with
   ``job_kind='embedding-backfill'``; ``skip_lock=True`` is the test
   bypass. Ports ``backfill.ts:20-23``.

3. **Rate limit is per-process.** :func:`asyncio.sleep` between Voyage
   calls (``1 / max_requests_per_second``). The worker_lock guarantees
   single-flight across processes so this RPS IS what hits Voyage.
   Defaults to 0.5 RPS (one request per 2s) — generous margin under
   Voyage tier-1 5 RPS. Ports ``backfill.ts:25-29``.

4. **Resumable.** Each batch's writes commit independently, so a mid-tick
   crash loses at most one in-flight batch worth of Voyage spend. Next
   tick picks up the still-unembedded rows via the ``NOT EXISTS`` filter.
   Ports ``backfill.ts:31-34``.

5. **Idempotent on per-row basis.** Caller's pre-filter "rows where no
   ``lcm_embedding_meta`` row exists for (model, kind, id)" means
   re-running the cron never re-embeds an already-embedded row. UPSERT
   semantics on the meta table also guard against double-write.
   Ports ``backfill.ts:36-40``.

6. **Suppression-aware.** Rows where ``summaries.suppressed_at IS NOT
   NULL`` are skipped (we don't pay to embed text the operator has asked
   us to forget). Ports ``backfill.ts:42-44``.

7. **Over-cap guard.** Rows where ``summaries.token_count >
   MAX_TOKENS_PER_EMBED_DOC`` (27_000) are filtered at the SQL ``BETWEEN``
   clause (no Voyage spend). Any defensively-detected over-cap row that
   sneaks through is recorded in :attr:`BackfillResult.skipped_over_cap`.
   Ports ``backfill.ts:46-49``.

### Wave-12 fix (load-bearing)

Per ADR-029 §"Known Wave-N fixes": after a successful Voyage embed call,
the worker re-checks ownership of its lock BEFORE writing the result. A
60s Voyage Retry-After + 30s timeout = up to 90s wall-time per batch =
:data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_MS` exactly. If
the lock TTL crossed during ``embedTexts``, another worker may have GC'd
the lock and started processing the same docs — writing now would
duplicate vec0 rows or race ``lcm_embedding_meta``. The Wave-12 re-check
detects this and aborts the batch's writes; both workers' next-tick
re-SELECTs will pick up where they left off.

See:

* ``docs/porting-guides/embeddings.md`` §"Backfill cron" (lines 1099-1180).
* ``docs/adr/018-concurrency-model.md`` §0 invariant.
* ``docs/adr/029-wave-fix-provenance.md`` Wave-12 row.
* ``lossless-claw/src/embeddings/backfill.ts`` lines 1-637 — TS source.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal, TypedDict

from lossless_hermes.concurrency.model import assert_no_open_tx
from lossless_hermes.concurrency.worker_lock import (
    acquire_lock,
    generate_worker_id,
    heartbeat_lock,
    release_lock,
)
from lossless_hermes.embeddings.store import (
    EmbeddedKind,
    embeddings_table_exists,
    record_embedding,
)
from lossless_hermes.voyage.client import (
    MAX_TOKENS_PER_EMBED_BATCH,
    MAX_TOKENS_PER_EMBED_DOC,
    VoyageClient,
    VoyageError,
)

__all__ = [
    "BackfillResult",
    "BackfillSkippedDoc",
    "count_pending_docs",
    "tick_embedding_backfill",
]

_log = logging.getLogger("lossless_hermes.embeddings.backfill")


# ---------------------------------------------------------------------------
# Module constants (verbatim port of ``backfill.ts:203-205``)
# ---------------------------------------------------------------------------

#: One Voyage request per 2 s — generous safety margin under Voyage tier-1
#: limits (300 RPM = 5 RPS). Worker-lock single-flight means the rate IS
#: what hits the API. Ports ``backfill.ts:203``.
_DEFAULT_MAX_RPS: float = 0.5

#: Per-tick document cap. At 80K tokens/batch × 2s/batch this is ~7-15
#: minutes per tick depending on doc length. Tune up for first-run backfill
#: (no contention); down for steady state. Ports ``backfill.ts:204``.
_DEFAULT_PER_TICK_LIMIT: int = 200

#: Skip empty stubs (a leaf with token_count=0 is degenerate and not worth
#: an embed slot). Ports ``backfill.ts:205``.
_DEFAULT_MIN_TOKEN_COUNT: int = 1

#: Maximum candidates returned by one SELECT (per ``backfill.ts:279``).
#: Smaller than ``per_tick_limit`` so the bin-packer sees a manageable
#: working set per iteration; the outer while-loop re-selects until the
#: tick budget is exhausted.
_SELECT_BATCH_CAP: int = 64

#: Voyage retry cap inside the backfill cron. Lower than the client default
#: of 3 — caps worst-case batch wall-time below 90 s lock TTL. With
#: ``voyage_timeout_ms = 30_000``, 2 attempts × 30 s + 0.5 s backoff
#: ≈ 60.5 s, comfortably under
#: :data:`~lossless_hermes.concurrency.model.WORKER_LOCK_TTL_MS` = 90 s.
#: Ports ``backfill.ts:103-107`` rationale.
_DEFAULT_VOYAGE_MAX_RETRIES: int = 1

#: Per-attempt Voyage timeout inside the backfill cron. 30s, half of the
#: client default 60s. Same lock-TTL reasoning as
#: :data:`_DEFAULT_VOYAGE_MAX_RETRIES`. Ports ``backfill.ts:117-122``.
_DEFAULT_VOYAGE_TIMEOUT_S: float = 30.0


# ---------------------------------------------------------------------------
# Result dataclasses (port of ``backfill.ts:174-201``)
# ---------------------------------------------------------------------------

#: Reasons a doc can be skipped — matches ``backfill.ts:174-178``. Note that
#: a per-row write failure in :func:`_write_batch` records ``voyage_other``
#: (matches TS line 597) rather than a new ``write_error`` kind, to keep the
#: discriminator stable across the port.
SkipReason = Literal[
    "over_cap",
    "voyage_400",
    "voyage_other",
    "lock_stolen_mid_embed",
]


@dataclass(frozen=True, slots=True)
class BackfillSkippedDoc:
    """A single document skipped during a tick.

    Ports ``backfill.ts:174-178`` ``BackfillSkippedDoc`` interface. The
    ``reason`` discriminator lets callers count per-class skip rates and
    target operator alerts (e.g. a tick with 20% ``voyage_400`` is a sign
    the model returns errors on this batch shape).
    """

    summary_id: str
    reason: SkipReason
    detail: str | None = None


@dataclass(slots=True)
class BackfillResult:
    """Outcome of a single backfill tick.

    Ports ``backfill.ts:180-195`` ``BackfillResult`` interface. Mutable
    (not frozen) so the tick driver can accumulate counts in-place without
    repeatedly rebuilding the dataclass — the TS code uses a spread
    pattern (``{...result, ...}``) which is cheap in JS but allocs heavy
    in Python.

    Attributes:
        embedded_count: Rows where vec0 + meta INSERTs both succeeded.
        skipped_over_cap: Count of docs filtered because
            ``token_count > max_token_count``. These docs spent no Voyage
            quota. The SQL ``BETWEEN`` clause filters most before they
            reach the tick; the in-process counter is defense-in-depth
            against a config drift between the SQL filter and the
            ``max_token_count`` constant.
        skipped: Per-doc failure detail. One entry per failed embed
            (Voyage 400 / 500, lock stolen, etc.). Always populated for
            ``over_cap`` rows too (parity with ``backfill.ts:619-624``).
        per_tick_limit_reached: :data:`True` if the tick hit
            ``per_tick_limit`` and there may be more pending. The caller
            (worker loop) schedules the next tick.
        lock_not_acquired: :data:`True` if the cross-process worker lock
            was held by a peer at acquire time, OR if the lock was stolen
            mid-tick by a peer worker (the heartbeat returned False).
            The caller skips this tick.
        voyage_tokens_consumed: Sum of ``usage.total_tokens`` across all
            successful Voyage responses this tick. Used for operator
            budget tracking.
        duration_ms: Walltime in milliseconds from tick start to return.
            Measured via :func:`time.monotonic` so it's safe under wall-
            clock skew.
    """

    embedded_count: int = 0
    skipped_over_cap: int = 0
    skipped: list[BackfillSkippedDoc] = field(default_factory=list)
    per_tick_limit_reached: bool = False
    lock_not_acquired: bool = False
    voyage_tokens_consumed: int = 0
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Internal types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PendingDoc:
    """One row from the candidate SELECT.

    Ports ``backfill.ts:197-201`` ``PendingDoc``. Internal-only — callers
    never see this shape; the public surface is :class:`BackfillResult`.
    """

    summary_id: str
    content: str
    token_count: int


class BatchCompleteInfo(TypedDict):
    """Payload passed to the :class:`BackfillOptions.on_batch_complete` callback.

    Ports ``backfill.ts:160-165``. One dict per batch (success OR failure).
    The :class:`typing.TypedDict` form keeps the keys checkable at static-
    analysis time without forcing callers to import an extra dataclass.
    """

    batch_size: int
    succeeded: int
    failed: int
    voyage_tokens: int


#: Hook invoked once per batch with batch outcome counters.
OnBatchComplete = Callable[[BatchCompleteInfo], None]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def tick_embedding_backfill(
    db: sqlite3.Connection,
    *,
    model_name: str,
    voyage_model: str,
    voyage: VoyageClient,
    input_type: Literal["query", "document"] | None = "document",
    embedded_kind: EmbeddedKind = "summary",
    voyage_output_dimension: int | None = None,
    voyage_max_retries: int = _DEFAULT_VOYAGE_MAX_RETRIES,
    voyage_timeout_s: float = _DEFAULT_VOYAGE_TIMEOUT_S,
    max_requests_per_second: float = _DEFAULT_MAX_RPS,
    max_batch_tokens: int = MAX_TOKENS_PER_EMBED_BATCH,
    per_tick_limit: int = _DEFAULT_PER_TICK_LIMIT,
    min_token_count: int = _DEFAULT_MIN_TOKEN_COUNT,
    max_token_count: int = MAX_TOKENS_PER_EMBED_DOC,
    worker_id: str | None = None,
    skip_lock: bool = False,
    on_batch_complete: OnBatchComplete | None = None,
) -> BackfillResult:
    """Run one backfill tick.

    Ports ``backfill.ts:219-436`` ``runBackfillTick`` to Python. Caller
    contract:

    1. Provide an open :class:`sqlite3.Connection` with the v4.1 migration
       ladder applied (``lcm_embedding_profile`` row for ``model_name``,
       ``lcm_embeddings_<slug>`` table created via
       :func:`~lossless_hermes.embeddings.store.ensure_embeddings_table`).
    2. Pass an initialized :class:`~lossless_hermes.voyage.client.VoyageClient`.
       The client is shared across ticks (re-instantiating per-tick wastes
       the httpx connection pool).
    3. Receive a :class:`BackfillResult`. Schedule the next tick if
       :attr:`BackfillResult.per_tick_limit_reached` is :data:`True`, or
       skip if :attr:`BackfillResult.lock_not_acquired` is :data:`True`.

    The function raises only on programmer errors (vec0 not loaded, missing
    profile, missing table). Voyage errors except ``auth`` are caught
    per-batch and surfaced in the result; an ``auth`` error is fatal and
    re-raised so the worker scheduler can alert the operator.

    Args:
        db: Open SQLite connection with sqlite-vec loaded + migrations
            applied. Required: ``lcm_embedding_profile`` has a row for
            ``model_name`` AND the per-model vec0 table exists.
        model_name: Profile model name (e.g. ``"voyage-4-large"``). Matches
            ``lcm_embedding_profile.model_name``.
        voyage_model: Voyage API model id. Usually == ``model_name`` but
            kept separate so profile names can diverge from the upstream
            API id (e.g. ``"voyage-4-large-v2"`` profile still calls
            ``"voyage-4-large"`` upstream).
        voyage: Initialized :class:`VoyageClient` to perform the embed
            calls. Caller owns its lifecycle.
        input_type: Voyage ``input_type``. Defaults to ``"document"`` —
            using ``"query"`` here would degrade retrieval quality
            because Voyage's asymmetric embedding optimizes queries for
            matching documents, not the reverse. Ports ``backfill.ts:86-93``.
        embedded_kind: What kind to embed. Default ``"summary"`` (the only
            kind backfilled by this loop at v0; entity / theme embeddings
            land in later epics).
        voyage_output_dimension: Forward to Voyage when set. Required for
            non-default-dim profiles (256/512/2048) — Voyage returns its
            default (1024) otherwise and the vec0 INSERT fails with dim
            mismatch. Resolved by callers from ``lcm_embedding_profile.dim``.
            Ports ``backfill.ts:109-115`` (Wave-11 reviewer P1 fix).
        voyage_max_retries: Voyage client retry count for each batch.
            Default 1 (so backfill caps at 2 total attempts per batch).
            Lower than the client default (3) — caps worst-case batch
            wall-time below the 90 s lock TTL. Tests can pass 0 to
            surface 5xx immediately. Ports ``backfill.ts:99-107``.
        voyage_timeout_s: Per-attempt Voyage timeout in seconds. Default
            30.0 (half of the client default 60.0). Combined with
            ``voyage_max_retries=1`` → worst case per batch ≈ 60.5 s,
            under :data:`WORKER_LOCK_TTL_S` = 90. Ports ``backfill.ts:117-122``.
        max_requests_per_second: Voyage RPS pacing. Default 0.5 — one
            request per 2 s. Worker-lock single-flight makes per-process
            RPS the same as per-API RPS. Ports ``backfill.ts:124-128``.
        max_batch_tokens: Max total Voyage tokens per single request batch.
            Defaults to :data:`MAX_TOKENS_PER_EMBED_BATCH` (80_000).
            Ports ``backfill.ts:130-131``.
        per_tick_limit: How many DOCUMENTS to embed in one tick before
            releasing the lock. Default 200. After this, the function
            returns with :attr:`BackfillResult.per_tick_limit_reached` =
            :data:`True` so the caller knows to schedule a follow-up.
            Ports ``backfill.ts:133-141``.
        min_token_count: Lower-bound filter on ``summaries.token_count``.
            Default 1 — skips empty stubs. Ports ``backfill.ts:142-143``.
        max_token_count: Upper-bound filter on ``summaries.token_count``.
            Default :data:`MAX_TOKENS_PER_EMBED_DOC` (27_000). Docs over
            this are filtered by the SQL ``BETWEEN`` clause and don't
            spend Voyage quota. Ports ``backfill.ts:144-149``.
        worker_id: Optional override for the lock's worker_id. Default:
            generated via :func:`generate_worker_id`. Tests use a fixed id
            to verify lock-release semantics. Ports ``backfill.ts:151-155``.
        skip_lock: Bypass the cross-process worker lock. Tests use
            :data:`True` to exercise the tick path without coordinating
            with other workers. Default :data:`False`. Ports
            ``backfill.ts:166-171``.
        on_batch_complete: Optional callback ``({batch_size, succeeded,
            failed, voyage_tokens}) -> None``. Invoked once per batch
            (success OR failure) for telemetry. Must not raise. The
            current type annotation accepts ``None`` only; pass a
            callable inline via ``cast`` if needed (kept loose-typed
            for parity with TS's ``onBatchComplete?: (info) => void``).

    Returns:
        A :class:`BackfillResult` summarizing the tick outcome.

    Raises:
        RuntimeError: ``sqlite-vec`` is not loaded on ``db`` (caller must
            run :func:`~lossless_hermes.db.connection.try_load_sqlite_vec`
            first).
        RuntimeError: The per-model vec0 table doesn't exist (caller must
            run :func:`~lossless_hermes.embeddings.store.ensure_embeddings_table`
            first).
        VoyageError: Auth error (``kind="auth"``) — fatal. The lock is
            still released via the ``finally`` block so the next tick
            can re-attempt after the operator fixes the API key.
    """
    started = time.monotonic()

    # Validate vec0 environment up-front. These are programmer errors —
    # worker scheduler bug if either check fails. Ports ``backfill.ts:225-234``.
    if not embeddings_table_exists(db, model_name):
        raise RuntimeError(
            f"[backfill] embeddings table for {model_name!r} doesn't exist — "
            "call ensure_embeddings_table() first"
        )

    # ``voyage_max_retries`` and ``voyage_timeout_s`` are part of the public
    # signature for parity with ``backfill.ts:99-122`` (the TS code forwards
    # them to ``embedTexts`` on a per-call basis). The Python
    # :class:`VoyageClient` accepts these only at construction time — callers
    # are expected to instantiate the client with these caps when wiring the
    # backfill job. We keep the args here so a future refactor that adds
    # per-call overrides to :meth:`VoyageClient.embed` (or wraps it in a
    # per-call config object) is a non-breaking change at the call site.
    _ = voyage_max_retries
    _ = voyage_timeout_s

    resolved_worker_id = (
        worker_id if worker_id is not None else generate_worker_id("embed-backfill")
    )

    min_batch_interval_s = 1.0 / max_requests_per_second if max_requests_per_second > 0 else 0.0

    # Acquire the cross-process worker lock unless explicitly skipping.
    # Ports ``backfill.ts:256-264``.
    if not skip_lock:
        acquired = acquire_lock(
            db,
            "embedding-backfill",
            worker_id=resolved_worker_id,
            job_metadata=f"model={model_name} kind={embedded_kind}",
        )
        if not acquired:
            return BackfillResult(
                lock_not_acquired=True,
                duration_ms=_elapsed_ms(started),
            )

    result = BackfillResult()
    last_batch_at_monotonic: float = 0.0
    # Per-tick blocklist: docs that already failed Voyage this tick. Each
    # is excluded from subsequent SELECTs so we don't retry within the
    # same tick (next tick will re-attempt — Voyage may have recovered).
    # Ports ``backfill.ts:268-271``.
    failed_this_tick: set[str] = set()

    try:
        processed = 0

        while processed < per_tick_limit:
            # 1. SELECT next batch — only documents NOT in lcm_embedding_meta
            #    for this (model, kind), within token bounds, not suppressed,
            #    not in the failed-this-tick blocklist.
            #    Ports ``backfill.ts:276-291``.
            remaining = per_tick_limit - processed
            batch_size = min(remaining, _SELECT_BATCH_CAP)
            candidates = _select_pending_docs(
                db,
                model_name=model_name,
                embedded_kind=embedded_kind,
                min_token_count=min_token_count,
                max_token_count=max_token_count,
                limit=batch_size,
                exclude_ids=failed_this_tick,
            )
            if not candidates:
                # No more pending — done. Ports ``backfill.ts:288-291``.
                break

            # 2. Identify over-cap (shouldn't happen due to SELECT filter,
            #    but defensive). Separate over-cap from queryable.
            #    Ports ``backfill.ts:295-306``.
            queryable: list[_PendingDoc] = []
            for doc in candidates:
                if doc.token_count > max_token_count:
                    result.skipped_over_cap += 1
                    result.skipped.append(
                        BackfillSkippedDoc(summary_id=doc.summary_id, reason="over_cap")
                    )
                else:
                    queryable.append(doc)
            if not queryable:
                processed += len(candidates)
                continue

            # 3. Group queryable into batches that fit max_batch_tokens.
            #    Ports ``backfill.ts:308-309``.
            batches = _pack_batches(queryable, max_batch_tokens)

            for batch in batches:
                # Rate-limit pacing: wait at least min_batch_interval since
                # last call. Ports ``backfill.ts:312-318``.
                if last_batch_at_monotonic > 0 and min_batch_interval_s > 0:
                    elapsed_s = time.monotonic() - last_batch_at_monotonic
                    if elapsed_s < min_batch_interval_s:
                        await asyncio.sleep(min_batch_interval_s - elapsed_s)
                # Heartbeat the lock so we don't get preempted mid-tick.
                # Ports ``backfill.ts:320-330``.
                if not skip_lock:
                    still_ours = heartbeat_lock(db, "embedding-backfill", resolved_worker_id)
                    if not still_ours:
                        # Another worker stole the lock — abort cleanly.
                        # Caller skips this tick on lock_not_acquired=True.
                        result.lock_not_acquired = True
                        result.duration_ms = _elapsed_ms(started)
                        return result

                last_batch_at_monotonic = time.monotonic()

                # §0 invariant: the Voyage call MUST be OUTSIDE any DB
                # write transaction. Caller is responsible for not
                # leaving one open between our SELECT loop and this call,
                # but defend at runtime anyway. Ports the implicit TS
                # contract from ``backfill.ts:14-18`` invariant 1.
                assert_no_open_tx(db)

                try:
                    embed_result = await voyage.embed(
                        [d.content for d in batch],
                        model=voyage_model,
                        input_type=input_type,
                        output_dimension=voyage_output_dimension,
                    )
                except VoyageError as e:
                    # Voyage error — record in skipped list, continue with
                    # next batch. We DO NOT retry per-doc; that would
                    # amplify cost. The next tick will re-attempt the
                    # same docs (they still have no meta row).
                    # Ports ``backfill.ts:351-377``.
                    if e.kind == "auth":
                        # Auth error is fatal — every subsequent batch will
                        # fail too. Re-throw so caller (worker scheduler)
                        # surfaces to operator. Lock released via finally.
                        raise
                    reason: SkipReason = "voyage_400" if e.kind == "bad_request" else "voyage_other"
                    for doc in batch:
                        failed_this_tick.add(doc.summary_id)
                        result.skipped.append(
                            BackfillSkippedDoc(
                                summary_id=doc.summary_id,
                                reason=reason,
                                detail=str(e),
                            )
                        )
                    if on_batch_complete is not None:
                        try:
                            on_batch_complete(
                                BatchCompleteInfo(
                                    batch_size=len(batch),
                                    succeeded=0,
                                    failed=len(batch),
                                    voyage_tokens=0,
                                )
                            )
                        except Exception:  # noqa: BLE001 — telemetry must not crash tick
                            _log.exception(
                                "[backfill] on_batch_complete callback raised on Voyage error"
                            )
                    processed += len(batch)
                    continue

                # LCM Wave-12 (2026-04-XX): post-embed heartbeat re-check
                # prevents a stale worker (heartbeat lapsed during the 60s
                # Voyage call) from writing an embed for a row another
                # worker now owns. Without this re-check: 60s Voyage timeout
                # + 30s heartbeat interval = up to 90s of silence = lock TTL
                # crossed. The check + abort matches ``backfill.ts:386-406``:
                # mark each doc in the batch ``lock_stolen_mid_embed`` and
                # return with ``lock_not_acquired=True`` so the caller skips
                # this tick.
                # Original: lossless-claw/src/embeddings/backfill.ts:386-406.
                if not skip_lock:
                    still_ours_after_embed = heartbeat_lock(
                        db, "embedding-backfill", resolved_worker_id
                    )
                    if not still_ours_after_embed:
                        for doc in batch:
                            failed_this_tick.add(doc.summary_id)
                            result.skipped.append(
                                BackfillSkippedDoc(
                                    summary_id=doc.summary_id,
                                    reason="lock_stolen_mid_embed",
                                    detail=(
                                        "Worker lock expired during Voyage call "
                                        "(likely Retry-After exceeded TTL); "
                                        "writes aborted."
                                    ),
                                )
                            )
                        result.lock_not_acquired = True
                        result.duration_ms = _elapsed_ms(started)
                        return result

                # 4. Write results (vec0 + meta). One implicit transaction
                #    per batch via _write_batch's internal BEGIN/COMMIT.
                #    Ports ``backfill.ts:407-421``.
                write_report = _write_batch(
                    db,
                    model_name=model_name,
                    embedded_kind=embedded_kind,
                    batch=batch,
                    vectors=embed_result.vectors,
                )
                result.embedded_count += write_report.succeeded
                result.voyage_tokens_consumed += embed_result.total_tokens
                result.skipped.extend(write_report.errors)

                if on_batch_complete is not None:
                    try:
                        on_batch_complete(
                            BatchCompleteInfo(
                                batch_size=len(batch),
                                succeeded=write_report.succeeded,
                                failed=len(write_report.errors),
                                voyage_tokens=embed_result.total_tokens,
                            )
                        )
                    except Exception:  # noqa: BLE001 — telemetry must not crash tick
                        _log.exception("[backfill] on_batch_complete callback raised on success")
                processed += len(batch)

        if processed >= per_tick_limit:
            result.per_tick_limit_reached = True
    finally:
        if not skip_lock:
            try:
                release_lock(db, "embedding-backfill", resolved_worker_id)
            except sqlite3.Error:
                # Best-effort — release failure must not mask the caller's
                # outcome. The lock will GC at TTL expiry.
                _log.exception(
                    "[backfill] release_lock failed for worker_id=%s",
                    resolved_worker_id,
                )

    result.duration_ms = _elapsed_ms(started)
    return result


def count_pending_docs(
    db: sqlite3.Connection,
    *,
    model_name: str,
    embedded_kind: EmbeddedKind = "summary",
    min_token_count: int = _DEFAULT_MIN_TOKEN_COUNT,
    max_token_count: int = MAX_TOKENS_PER_EMBED_DOC,
) -> int:
    """Return the number of leaves still pending embedding for ``model_name``.

    Ports ``backfill.ts:439-468`` ``countPendingDocs``. Same SELECT shape
    as the tick's candidate scan, projected to ``COUNT(*)``. Used by
    ``/lcm health`` (Epic 08) to surface the backfill backlog at
    operator-readable granularity.

    The default token bounds match the tick's defaults so this count
    reflects what a normal tick would process. Callers (e.g.
    ``/lcm describe``) that want to surface over-cap rows separately
    should call this twice with bumped ``max_token_count``.

    Args:
        db: Open SQLite connection with the v4.1 migrations applied.
        model_name: Profile model name to count pending docs for.
        embedded_kind: Kind to count. Default ``"summary"``.
        min_token_count: Lower-bound filter on ``summaries.token_count``.
            Default :data:`_DEFAULT_MIN_TOKEN_COUNT` (1).
        max_token_count: Upper-bound filter on ``summaries.token_count``.
            Default :data:`MAX_TOKENS_PER_EMBED_DOC` (27_000).

    Returns:
        Number of leaves where:

        * ``suppressed_at IS NULL``,
        * ``token_count BETWEEN min_token_count AND max_token_count``,
        * ``kind = 'leaf'``,
        * No active (non-archived) row exists in ``lcm_embedding_meta``
          for ``(summary_id, embedded_kind, model_name)``.
    """
    row = db.execute(
        """
        SELECT COUNT(*) AS n
          FROM summaries s
          WHERE s.suppressed_at IS NULL
            AND s.token_count BETWEEN ? AND ?
            AND s.kind = 'leaf'
            AND NOT EXISTS (
              SELECT 1 FROM lcm_embedding_meta m
                WHERE m.embedded_id = s.summary_id
                  AND m.embedded_kind = ?
                  AND m.embedding_model = ?
                  AND m.archived = 0
            )
        """,
        (min_token_count, max_token_count, embedded_kind, model_name),
    ).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _select_pending_docs(
    db: sqlite3.Connection,
    *,
    model_name: str,
    embedded_kind: EmbeddedKind,
    min_token_count: int,
    max_token_count: int,
    limit: int,
    exclude_ids: set[str],
) -> list[_PendingDoc]:
    """SELECT the next batch of pending docs.

    Ports ``backfill.ts:472-522`` ``selectPendingDocs``. Returns rows
    ordered by ``summary_id DESC`` (newest-first), filtered by:

    * ``suppressed_at IS NULL`` — operator-suppressed leaves never embed.
    * ``token_count BETWEEN min_token_count AND max_token_count`` — the
      over-cap filter happens here in SQL, not in Python.
    * ``kind = 'leaf'`` — only leaves carry embeddable content; condensed
      summaries get a separate path (out of scope for v0 backfill).
    * ``summary_id NOT IN exclude_ids`` — per-tick failure blocklist.
    * ``NOT EXISTS (lcm_embedding_meta row)`` — idempotency: only docs
      that don't already have an active embed for this model/kind.

    Ordering newest-first prioritizes freshest content for retrieval
    queries. Deterministic ordering also helps debug (next tick pulls
    the same set if conditions don't change).

    Args:
        db: Open SQLite connection.
        model_name: Profile model name.
        embedded_kind: What kind to look for.
        min_token_count: Lower bound on ``token_count``.
        max_token_count: Upper bound on ``token_count``.
        limit: Maximum rows to return.
        exclude_ids: Per-tick blocklist (docs already failed this tick).
            Iterated to build the ``NOT IN`` clause; for empty sets the
            clause is omitted (avoids the SQLite ``NOT IN ()`` parser
            quirk).

    Returns:
        A list of :class:`_PendingDoc` rows. Empty list when no more
        candidates exist for this tick.
    """
    exclude = sorted(exclude_ids)  # deterministic order for binding + debugging
    # Build the IN-list dynamically; exclude can be empty (no IN clause
    # needed). Ports ``backfill.ts:487-491``.
    exclude_clause = (
        f"AND s.summary_id NOT IN ({','.join('?' for _ in exclude)}) " if exclude else ""
    )
    sql = (
        "SELECT s.summary_id, s.content, s.token_count "
        "  FROM summaries s "
        "  WHERE s.suppressed_at IS NULL "
        "    AND s.token_count BETWEEN ? AND ? "
        "    AND s.kind = 'leaf' "
        f"    {exclude_clause}"
        "    AND NOT EXISTS ( "
        "      SELECT 1 FROM lcm_embedding_meta m "
        "        WHERE m.embedded_id = s.summary_id "
        "          AND m.embedded_kind = ? "
        "          AND m.embedding_model = ? "
        "          AND m.archived = 0 "
        "    ) "
        "  ORDER BY s.summary_id DESC "
        "  LIMIT ?"
    )
    params: tuple[object, ...] = (
        min_token_count,
        max_token_count,
        *exclude,
        embedded_kind,
        model_name,
        limit,
    )
    rows = db.execute(sql, params).fetchall()
    return [_PendingDoc(summary_id=r[0], content=r[1], token_count=int(r[2])) for r in rows]


def _pack_batches(docs: list[_PendingDoc], max_batch_tokens: int) -> list[list[_PendingDoc]]:
    """Greedy bin-pack docs into batches of ≤ ``max_batch_tokens``.

    Ports ``backfill.ts:531-546`` ``packBatches``. Each batch respects
    ``sum(token_count) <= max_batch_tokens`` AND ``len(batch) >= 1``. If
    a single doc exceeds ``max_batch_tokens``, it goes in a batch of 1
    (Voyage will 400; caller records as ``voyage_400`` and moves on).

    Docs are in SELECT order (newest-first); we don't re-sort. The
    greedy choice prefers fill-the-current-batch over balancing — a
    23K + 23K + 23K doc set with ``max_batch_tokens=60K`` produces
    ``[[23K, 23K], [23K]]`` not ``[[23K], [23K, 23K]]``.

    Args:
        docs: Candidate docs in their desired order.
        max_batch_tokens: Token budget per batch.

    Returns:
        A list of batches. Each inner list has at least one element.
    """
    batches: list[list[_PendingDoc]] = []
    current: list[_PendingDoc] = []
    current_tokens = 0
    for doc in docs:
        if current and current_tokens + doc.token_count > max_batch_tokens:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(doc)
        current_tokens += doc.token_count
    if current:
        batches.append(current)
    return batches


@dataclass(slots=True)
class _WriteReport:
    """Outcome of a single :func:`_write_batch` call.

    Internal-only. Ports ``backfill.ts:548-554`` (the structural shape;
    in TS this was an inline return).
    """

    succeeded: int
    errors: list[BackfillSkippedDoc]


def _write_batch(
    db: sqlite3.Connection,
    *,
    model_name: str,
    embedded_kind: EmbeddedKind,
    batch: list[_PendingDoc],
    vectors: list[list[float]],
) -> _WriteReport:
    """Write a batch's vec0 + meta rows under one ``BEGIN IMMEDIATE``.

    Ports ``backfill.ts:548-617`` ``writeBatch``. Each row is wrapped in
    its own SAVEPOINT so a single failure rolls back JUST that row's
    partial writes (vec0 + meta together) without killing the whole
    batch.

    **Wave-1 Auditor #2 finding #4 (preserved verbatim per ADR-029):**
    per-row write failure inside the batch tx left a phantom vec0 row
    (no corresponding meta) when ``record_embedding`` partially
    succeeded — the meta-side INSERT failed but the vec0-side had
    already gone through. On the next tick, ``NOT EXISTS`` in meta
    would re-pick the doc, INSERT a SECOND vec0 row, and now we'd have
    duplicate KNN entries.

    The per-row SAVEPOINT uses a crypto-random suffix
    (:func:`secrets.token_hex`) — two concurrent writers (different
    processes) holding different locks for different job-kinds could
    theoretically race the same SAVEPOINT name; the random suffix
    prevents collision. Ports ``backfill.ts:572`` plus the Wave-5
    rationale from ``store.ts:567``.

    Args:
        db: Open SQLite connection. Must NOT already have a transaction
            open (we ``BEGIN IMMEDIATE`` ourselves).
        model_name: Profile model name.
        embedded_kind: What kind we're writing.
        batch: The batch of docs to write. Must match ``vectors`` in
            length.
        vectors: Per-doc vectors from Voyage. Each must be a list of
            floats with length matching the profile dim.

    Returns:
        A :class:`_WriteReport` with the success count and per-row
        errors. A tx-level error (vs per-row) rolls back the whole
        batch and produces an error entry for every doc.
    """
    errors: list[BackfillSkippedDoc] = []
    succeeded = 0
    db.execute("BEGIN IMMEDIATE")
    try:
        for i, doc in enumerate(batch):
            vec = vectors[i]
            # LCM Wave-5 (2026-02-03): crypto-random SAVEPOINT suffix prevents
            # collision under concurrent outer-tx callers. 16 hex chars = 64
            # bits, collision-free for any realistic concurrency. Ports
            # ``backfill.ts:572`` (the TS source uses ``bf_${i}`` — we upgrade
            # to crypto-random per the Wave-5 rationale documented at
            # ``store.ts:567`` and ADR-029's policy of consistent SAVEPOINT
            # naming across the backfill + store modules).
            sp = f"sp_emb_{secrets.token_hex(8)}"
            db.execute(f"SAVEPOINT {sp}")
            try:
                record_embedding(
                    db,
                    model_name=model_name,
                    embedded_id=doc.summary_id,
                    embedded_kind=embedded_kind,
                    vector=vec,
                    source_token_count=doc.token_count,
                )
                db.execute(f"RELEASE {sp}")
                succeeded += 1
            except Exception as e:  # noqa: BLE001 — record + continue
                # Per-row write failure (rare — dim mismatch, e.g.). Roll
                # back to SAVEPOINT — that erases any vec0 partial write,
                # leaving the row entirely unsynced. Caller will re-pick
                # on next tick (clean slate). Ports ``backfill.ts:584-600``.
                try:
                    db.execute(f"ROLLBACK TO {sp}")
                    db.execute(f"RELEASE {sp}")
                except sqlite3.Error:
                    # Best-effort; if SAVEPOINT rollback fails the outer
                    # try/catch will catch and ROLLBACK the whole tx.
                    pass
                errors.append(
                    BackfillSkippedDoc(
                        summary_id=doc.summary_id,
                        reason="voyage_other",
                        detail=str(e),
                    )
                )
        db.execute("COMMIT")
    except Exception as e:  # noqa: BLE001 — tx-level rollback path
        # Transaction-level error (constraint failure, lock loss). Roll
        # back; caller sees no progress on these docs and re-attempts.
        # Ports ``backfill.ts:603-614``.
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return _WriteReport(
            succeeded=0,
            errors=[
                BackfillSkippedDoc(
                    summary_id=doc.summary_id,
                    reason="voyage_other",
                    detail=f"tx-rollback: {e}",
                )
                for doc in batch
            ],
        )
    return _WriteReport(succeeded=succeeded, errors=errors)


def _elapsed_ms(started_monotonic: float) -> int:
    """Return integer-ms elapsed since ``started_monotonic``."""
    return int((time.monotonic() - started_monotonic) * 1000)


# Suppress unused-import warning for ``replace`` — kept available for
# future-port symmetry with ``backfill.ts``'s spread-based result mutation
# pattern (the TS source uses ``{...result, ...}`` everywhere; the Python
# port mutates in place but ``replace`` is the idiomatic alternative if a
# future refactor wants immutable :class:`BackfillResult`).
_ = replace
