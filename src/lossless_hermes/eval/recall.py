"""Retrieval recall@K — LCM v4.1 §11 / D.03.

Ports ``lossless-claw/src/eval/recall.ts`` (LCM commit ``1f07fbd`` on
branch ``pr-613``, 237 LOC TS → ~330 LOC Python with docstrings +
Wave-N comments).

Pure metric module. Given a corpus of :class:`QueryRecord` (each with
optional ``expected_summary_ids``) and an injected
:class:`RecallSearchAdapter`, computes:

* per-query recall@K for K ∈ ``k_values``
* per-query reciprocal rank (1/rank of first hit, 0 if none)
* aggregate per-stratum + overall means

**NO LLM CALLS.** Deterministic given the adapter — tests use synthetic
adapters that return canned hit lists.

The wiring to actually call semantic-search/hybrid-search is a Group F
concern (the ``/lcm eval`` command); this module just measures whatever
the adapter returns.

Recall@K convention used here::

    recallAtK = |hits[:K] ∩ expected| / |expected|

where the expected set is taken from the :class:`QueryRecord`. Queries
with no ``expected_summary_ids`` (or an empty list) are SKIPPED for
recall computation — their ``recall_at_k`` map is empty and they don't
contribute to the per-stratum / overall means. (They CAN still
contribute to synthesis quality eval; that's a separate metric in
``judge.ts`` which is **deferred** for v4.1 first cut.)

### Wave-N provenance comments preserved (per ADR-029)

This module preserves three Wave-N fixes from the TS source:

* **Wave-4 Auditor #15 P1** (``recall.ts:78-83``) — per-query timeout
  preventing pathological adapter hang.
* **Wave-5 P2** (``recall.ts:176-186``) — timeout clamp at min 100ms,
  max 5 minutes, to prevent operator misuse zeroing out every query.
* **Wave-9 Agent #10 P1** (``recall.ts:191-208``) — clear the timeout
  timer in ``finally`` so an N=1000 baseline run doesn't leak 1000
  pending timers.

See:

* ``epics/08-cli-ops/08-13-eval-runner.md`` — this issue.
* ``lossless-claw/src/eval/recall.ts:1-237`` — TS source.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Protocol

from lossless_hermes.eval.query_set import QueryRecord

__all__ = [
    "DEFAULT_K_VALUES",
    "RecallEvalOptions",
    "RecallReport",
    "RecallResult",
    "RecallSearchAdapter",
    "RecallStratumAggregate",
    "run_recall_eval",
]


DEFAULT_K_VALUES: tuple[int, ...] = (1, 5, 10, 20, 50)
"""TS ``DEFAULT_K_VALUES`` (``recall.ts:86``). Used when no ``k_values``
option is supplied."""


class RecallSearchAdapter(Protocol):
    """Caller-provided search adapter.

    Ports TS ``RecallSearchAdapter`` interface (``recall.ts:34-41``).

    The adapter is responsible for whatever retrieval mode is being
    measured (FTS-only, hybrid, semantic-only, etc.). The adapter's
    ``mode`` is opaque to this module.
    """

    async def search(self, query: QueryRecord) -> list[str]:
        """Given a query, return the IDs the search returned, in rank order.

        Best first. May return more or fewer than ``max(k_values)`` —
        recall@K truncates internally.
        """
        ...


@dataclass(frozen=True, slots=True)
class RecallResult:
    """Per-query recall + RR result.

    Ports TS ``RecallResult`` interface (``recall.ts:43-57``).

    Attributes:
        query_id: The query's id.
        hits: Hit list as returned by the adapter.
        expected: Ground-truth expected IDs (empty tuple if the query
            had none).
        recall_at_k: K → recall fraction (0..1). Empty if ``expected``
            is empty.
        reciprocal_rank: Reciprocal rank — 1 / (1-based rank of the
            first expected ID found anywhere in ``hits``). 0 if no
            expected ID was found. (Standard MRR formula;
            ``recall_at_k[1]`` is the binary version.)
    """

    query_id: str
    hits: tuple[str, ...]
    expected: tuple[str, ...]
    recall_at_k: dict[int, float]
    reciprocal_rank: float


@dataclass(frozen=True, slots=True)
class RecallStratumAggregate:
    """Aggregate recall + RR over a set of queries.

    Ports TS ``RecallStratumAggregate`` interface (``recall.ts:59-64``).

    Attributes:
        mean_recall_at_k: K → mean recall fraction (0..1).
        mean_rr: Mean reciprocal rank.
        n: Number of queries that contributed to these means (i.e.
            had expected IDs).
    """

    mean_recall_at_k: dict[int, float]
    mean_rr: float
    n: int


@dataclass(frozen=True, slots=True)
class RecallReport:
    """Full recall report — per-query + per-stratum + overall.

    Ports TS ``RecallReport`` interface (``recall.ts:66-72``).

    Attributes:
        per_query: One :class:`RecallResult` per query.
        by_stratum: Aggregates per stratum. Keys are the strata that
            had ≥1 scored query.
        overall: Overall aggregate across all scored queries.
    """

    per_query: tuple[RecallResult, ...]
    by_stratum: dict[str, RecallStratumAggregate]
    overall: RecallStratumAggregate


@dataclass(frozen=True, slots=True)
class RecallEvalOptions:
    """Options for :func:`run_recall_eval`.

    Ports TS ``RecallEvalOptions`` interface (``recall.ts:74-84``).

    Attributes:
        k_values: K values to compute recall at. ``None`` →
            :data:`DEFAULT_K_VALUES`.
        per_query_timeout_ms: Wave-4 Auditor #15 P1 fix: per-query
            timeout (ms). A pathological adapter (network hang, vec0
            deadlock) without this would hang the whole eval
            indefinitely. Default 30s; queries that exceed this are
            reported as failed (zero recall) and the eval continues.
    """

    k_values: Sequence[int] | None = None
    per_query_timeout_ms: float | None = None


def _compute_per_query(
    query_id: str,
    hits: Sequence[str],
    expected: Sequence[str],
    k_values: Sequence[int],
) -> RecallResult:
    """Ports TS ``computePerQuery`` (``recall.ts:88-122``)."""
    recall_at_k: dict[int, float] = {}
    if len(expected) > 0:
        expected_set = set(expected)
        for k in k_values:
            # Dedupe the window before counting intersection — if an
            # adapter ever returns the same ID twice (rare but
            # possible), we don't want recall > 1.
            window_set = set(hits[:k])
            intersect = sum(1 for hid in window_set if hid in expected_set)
            recall_at_k[k] = intersect / len(expected)

    reciprocal_rank = 0.0
    if len(expected) > 0:
        expected_set = set(expected)
        for i, hid in enumerate(hits):
            if hid in expected_set:
                reciprocal_rank = 1.0 / (i + 1)
                break

    return RecallResult(
        query_id=query_id,
        hits=tuple(hits),
        expected=tuple(expected),
        recall_at_k=recall_at_k,
        reciprocal_rank=reciprocal_rank,
    )


def _empty_aggregate(k_values: Sequence[int]) -> RecallStratumAggregate:
    """Ports TS ``emptyAggregate`` (``recall.ts:124-128``)."""
    return RecallStratumAggregate(
        mean_recall_at_k={k: 0.0 for k in k_values},
        mean_rr=0.0,
        n=0,
    )


def _aggregate(
    results: Sequence[RecallResult],
    k_values: Sequence[int],
) -> RecallStratumAggregate:
    """Ports TS ``aggregate`` (``recall.ts:130-151``)."""
    if len(results) == 0:
        return _empty_aggregate(k_values)

    sum_recall: dict[int, float] = {k: 0.0 for k in k_values}
    sum_rr = 0.0

    for r in results:
        for k in k_values:
            sum_recall[k] += r.recall_at_k.get(k, 0.0)
        sum_rr += r.reciprocal_rank

    mean_recall_at_k: dict[int, float] = {k: sum_recall[k] / len(results) for k in k_values}
    return RecallStratumAggregate(
        mean_recall_at_k=mean_recall_at_k,
        mean_rr=sum_rr / len(results),
        n=len(results),
    )


async def _race_with_timeout(
    coro: Awaitable[list[str]],
    timeout_s: float,
) -> list[str] | None:
    """Run ``coro`` with a timeout. Return ``None`` on timeout.

    Wave-9 Agent #10 P1 fix: in the TS source, the ``setTimeout`` was
    never cleared when the adapter resolved first, leaving a pending
    timer per query. Python's :func:`asyncio.wait_for` cancels the
    awaitable when the timer fires (or vice versa), so we don't have
    the timer-leak problem — but we mirror the structure so future
    maintenance recognizes the lineage.

    Adapter exceptions still bubble (``asyncio.wait_for`` propagates
    the wrapped exception).
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        return None


async def run_recall_eval(
    queries: Sequence[QueryRecord],
    adapter: RecallSearchAdapter,
    opts: RecallEvalOptions | None = None,
) -> RecallReport:
    """Run the full recall eval.

    Ports TS ``runRecallEval`` (``recall.ts:163-236``).

    Queries are processed sequentially through the adapter —
    concurrency is the adapter's call (most retrieval surfaces aren't
    safe to parallelize against the same SQLite connection).

    Adapter exceptions are NOT swallowed — if the adapter raises, the
    caller sees the error. This is deliberate: silently dropping a
    failed query would skew the aggregate.

    Args:
        queries: Queries to evaluate.
        adapter: Caller-provided retrieval adapter.
        opts: Optional configuration.

    Returns:
        The :class:`RecallReport`.

    Raises:
        ValueError: On empty ``k_values`` or non-positive K entries.
    """
    if opts is None:
        opts = RecallEvalOptions()

    requested_k = opts.k_values if opts.k_values is not None else DEFAULT_K_VALUES
    k_values = sorted(set(requested_k))
    if len(k_values) == 0:
        raise ValueError("k_values must be non-empty")
    for k in k_values:
        if not isinstance(k, int) or k < 1:
            raise ValueError(f"k_values entries must be positive integers (got {k})")

    # Wave-4 Auditor #15 P1 fix + Wave-5 P2 clamp: per-query timeout.
    # Default 30s. Clamp to ≥100ms — per_query_timeout_ms=0 would
    # resolve immediately and zero out every query's recall, with no
    # error signal. Cap at 5min to prevent operator misuse.
    requested_timeout_ms = opts.per_query_timeout_ms
    if (
        requested_timeout_ms is not None
        and isinstance(requested_timeout_ms, (int, float))
        and not _is_nan(float(requested_timeout_ms))
        and requested_timeout_ms >= 100
    ):
        per_query_timeout_s = min(float(requested_timeout_ms), 5 * 60 * 1000) / 1000.0
    else:
        per_query_timeout_s = 30.0

    per_query: list[RecallResult] = []
    for q in queries:
        expected = q.expected_summary_ids or ()
        # Wave-9 Agent #10 P1 fix: asyncio.wait_for cancels the awaitable
        # on timeout (Python equivalent of clearing the timer). The TS
        # source had to manually clearTimeout — we get the cleanup for
        # free here.
        hits = await _race_with_timeout(adapter.search(q), per_query_timeout_s)
        resolved_hits: list[str] = hits if hits is not None else []
        per_query.append(_compute_per_query(q.query_id, resolved_hits, expected, k_values))

    # Aggregate over queries that have ≥1 expected ID (others contribute
    # empty recall_at_k maps and 0 RR — both would skew the mean).
    scored = [r for r in per_query if len(r.expected) > 0]

    by_stratum_groups: dict[str, list[RecallResult]] = {}
    for r in scored:
        q = next((qq for qq in queries if qq.query_id == r.query_id), None)
        if q is None:
            continue
        by_stratum_groups.setdefault(q.stratum, []).append(r)

    by_stratum: dict[str, RecallStratumAggregate] = {
        stratum: _aggregate(results, k_values) for stratum, results in by_stratum_groups.items()
    }

    return RecallReport(
        per_query=tuple(per_query),
        by_stratum=by_stratum,
        overall=_aggregate(scored, k_values),
    )


def _is_nan(x: float) -> bool:
    """Tiny helper: detect NaN without importing math everywhere."""
    return x != x
