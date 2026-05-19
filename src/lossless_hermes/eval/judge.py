"""Synthesis-quality judging — LCM v4.1 §11 / D.03.

Ports ``lossless-claw/src/eval/judge.ts`` (LCM commit ``1f07fbd`` on
branch ``pr-613``, 191 LOC TS → ~200 LOC Python with docstrings).

LLM-as-judge ensemble harness. Per architecture-v4.1 §11 the production
gate uses an **ensemble of 3 different model families** voting — but the
module accepts any ``1..N`` :class:`JudgeEntry` list and aggregates
accordingly. Callers inject the judges; tests inject deterministic
mocks.

### Injection pattern (verbatim from TS ``judge.ts:9-14``)

Same shape as :mod:`lossless_hermes.synthesis.dispatch`'s
:class:`~lossless_hermes.synthesis.dispatch.LlmCall` Protocol — the
caller supplies the callable, tests inject deterministic mocks. **No
model wiring lives here**; the wiring is a Group F concern
(``#09-07`` / ``#09-08`` callers reuse the synthesis llm-adapter so the
port doesn't ship a second LLM client).

### Judge-failure handling (verbatim from TS ``judge.ts:16-29``)

A judge can:

* return a finite-number score → counted toward ``mean_score``.
* return ``None``/missing score → counted as a failure (judge "couldn't
  decide"); ``reason`` defaults to ``"no_decision"``.
* raise an exception → also counted as a failure; ``reason`` captures
  the error message (``f"judge_error: {err}"``); ``score`` is ``None``.
* return a non-finite number (``nan`` / ``inf`` / ``-inf``) → invalidated
  to ``None`` with ``reason=f"invalid_score: {value}"``.

Per-query ``mean_score`` is computed over only the judges that returned
a non-``None`` score. If ALL judges failed for a query, ``mean_score``
is ``None`` and the query is excluded from the overall mean.

The aggregate :attr:`QualityOverall.judge_failures` counts the **total**
number of judge failure events across ``(queries × judges)`` — NOT the
count of queries that had ≥1 failure.

### Concurrency contract (verbatim from TS ``judge.ts:127-139``)

Within a single query all judges fire in parallel via
:func:`asyncio.gather` — they're independent calls to different
external services. Across queries we run sequentially to avoid
stampeding the same judge endpoints; if you want intra-set concurrency,
batch ``candidates_by_query`` yourself and call this in chunks.

### Score range (verbatim from TS ``judge.ts:31-37``)

We don't enforce ``1..5`` on the judge return value (the rubric scale
may evolve); we only require the number to be finite. Callers are
responsible for prompting their judges into the expected range.

See:

* ``epics/09-eval/09-03-eval-judge.md`` — this issue.
* ``lossless-claw/src/eval/judge.ts:1-191`` — TS source.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Protocol

from lossless_hermes.eval.query_set import QueryRecord

__all__ = [
    "JudgeCall",
    "JudgeCallArgs",
    "JudgeCallResult",
    "JudgeEntry",
    "PerJudgeScore",
    "QualityOverall",
    "QualityReport",
    "QualityResult",
    "run_quality_eval",
]


@dataclass(frozen=True, slots=True)
class JudgeCallArgs:
    """Args passed to a single judge call.

    Ports the TS ``JudgeCallArgs`` interface (``judge.ts:41-46``).

    Attributes:
        query: The query text being evaluated.
        candidate: The candidate synthesis to score.
        reference: Optional reference text for grounded judging.
            Forwarded from :attr:`QueryRecord.reference_summary` when
            set; ``None`` otherwise.
    """

    query: str
    candidate: str
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class JudgeCallResult:
    """Result returned by a single judge call.

    Ports the TS ``JudgeCallResult`` interface (``judge.ts:48-53``).

    Attributes:
        score: Score, typically ``1..5``. ``None`` if the judge couldn't
            decide. Non-finite values (``nan`` / ``inf`` / ``-inf``) are
            invalidated to ``None`` by :func:`run_quality_eval`.
        reason: Brief human-readable reason — surfaced in the per-query
            report.
    """

    score: float | None
    reason: str


class JudgeCall(Protocol):
    """The injected judge callable.

    Ports the TS ``JudgeCall`` interface (``judge.ts:55-57``).

    Same injection shape as
    :class:`lossless_hermes.synthesis.dispatch.LlmCall` — production
    wires this to the Hermes-side LLM adapter
    (:mod:`lossless_hermes.synthesis.llm_adapter`); tests inject
    deterministic async mocks.

    The signature is async because the TS canonical source returns
    ``Promise<JudgeCallResult>``.
    """

    async def judge(self, args: JudgeCallArgs) -> JudgeCallResult: ...


@dataclass(frozen=True, slots=True)
class JudgeEntry:
    """A judge entry in the ensemble.

    Ports the TS ``JudgeEntry`` interface (``judge.ts:59-64``).

    Attributes:
        judge_id: Opaque identifier — typically the model family name,
            e.g. ``"claude-opus-4-7"`` or ``"gpt-5.4"``.
        call: The :class:`JudgeCall` callable for this judge.
    """

    judge_id: str
    call: JudgeCall


@dataclass(frozen=True, slots=True)
class PerJudgeScore:
    """One judge's score for one candidate.

    Ports the TS ``PerJudgeScore`` interface (``judge.ts:66-70``).

    Attributes:
        judge_id: The judge that produced this score.
        score: The finite-number score, or ``None`` if the judge failed
            (returned ``None``, raised, or returned a non-finite value).
        reason: Human-readable reason. ``"judge_error: ..."`` if the
            judge raised; ``"invalid_score: ..."`` if it returned a
            non-finite value; ``"no_decision"`` if it returned a
            ``None`` score with no reason; otherwise the judge's own
            ``reason`` string.
    """

    judge_id: str
    score: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class QualityResult:
    """Per-query quality result.

    Ports the TS ``QualityResult`` interface (``judge.ts:72-78``).

    Attributes:
        query_id: The query's id.
        candidate: The candidate synthesis that was scored.
        per_judge_scores: One :class:`PerJudgeScore` per judge in the
            ensemble, in ensemble order.
        mean_score: Mean of the non-``None`` judge scores. ``None`` if
            every judge failed for this query.
    """

    query_id: str
    candidate: str
    per_judge_scores: list[PerJudgeScore]
    mean_score: float | None


@dataclass(frozen=True, slots=True)
class QualityOverall:
    """Overall aggregate across all judged queries.

    Ports the TS inline ``overall`` object (``judge.ts:80-90``).

    Attributes:
        mean_score: Mean of per-query ``mean_score`` over queries that
            had ≥1 successful judge. ``0.0`` if there are no such
            queries.
        n: Number of queries that had ≥1 successful judge.
        judge_failures: Total judge-failure events across
            ``(queries × judges)`` — NOT the count of queries that had
            ≥1 failure.
    """

    mean_score: float
    n: int
    judge_failures: int


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Full quality report — per-query + overall.

    Ports the TS ``QualityReport`` interface (``judge.ts:80-90``).

    Attributes:
        per_query: One :class:`QualityResult` per judged query (queries
            with no candidate are skipped — see :func:`run_quality_eval`).
        overall: The :class:`QualityOverall` aggregate.
    """

    per_query: list[QualityResult]
    overall: QualityOverall


def _is_finite_number(x: object) -> bool:
    """Port of TS ``isFiniteNumber`` (``judge.ts:92-94``).

    TS checks ``typeof x === "number" && Number.isFinite(x)``. Python's
    equivalent must:

    * accept ``int`` and ``float`` (the rubric scale may use either),
    * reject ``nan`` / ``inf`` / ``-inf`` regardless of numeric type,
    * reject ``bool`` — ``bool`` is an ``int`` subclass in Python, but a
      judge returning ``True``/``False`` as a "score" is a bug, not a
      ``1``/``0`` rating. TS has no such ambiguity (``typeof true`` is
      ``"boolean"``), so excluding ``bool`` keeps parity with the TS
      ``typeof === "number"`` gate.
    """

    if isinstance(x, bool):
        return False
    if not isinstance(x, (int, float)):
        return False
    return math.isfinite(x)


async def _call_one_judge(entry: JudgeEntry, args: JudgeCallArgs) -> PerJudgeScore:
    """Run a single judge and normalise its result into a :class:`PerJudgeScore`.

    Ports TS ``callOneJudge`` (``judge.ts:96-125``).

    Failure modes (all yield ``score=None``):

    * the judge raises → ``reason="judge_error: {err}"``.
    * the judge returns a ``None`` score → ``reason`` is the judge's own
      ``reason`` or ``"no_decision"``.
    * the judge returns a non-finite score → ``reason="invalid_score: {value}"``.

    The exception type does NOT leak — only the message is captured.
    """

    try:
        result = await entry.call.judge(args)
    except Exception as err:  # noqa: BLE001 — vendor-specific judge exceptions vary
        return PerJudgeScore(
            judge_id=entry.judge_id,
            score=None,
            reason=f"judge_error: {err}",
        )

    if result.score is None:
        # Judge couldn't decide — the empty-string reason from a judge
        # is also treated as "no reason given" (mirrors TS's
        # `result.reason ?? "no_decision"`, where `??` only catches
        # null/undefined — but TS judges practically never return ""
        # here; an explicit reason like "no-decision" is preserved).
        return PerJudgeScore(
            judge_id=entry.judge_id,
            score=None,
            reason=result.reason if result.reason else "no_decision",
        )

    if not _is_finite_number(result.score):
        return PerJudgeScore(
            judge_id=entry.judge_id,
            score=None,
            reason=f"invalid_score: {result.score}",
        )

    return PerJudgeScore(
        judge_id=entry.judge_id,
        score=float(result.score),
        reason=result.reason if result.reason else "",
    )


async def run_quality_eval(
    queries: list[QueryRecord],
    candidates_by_query: dict[str, str],
    judges: list[JudgeEntry],
) -> QualityReport:
    """Run quality judging for a set of ``(query → candidate)`` pairs.

    Ports TS ``runQualityEval`` (``judge.ts:127-191``).

    Queries with no entry in ``candidates_by_query`` are **SKIPPED**:
    they contribute no per-query result and don't bump ``overall.n``.
    This is the common case when retrieval failed and there's nothing
    to judge.

    Within a single query all judges fire in parallel via
    :func:`asyncio.gather` — they're independent calls to different
    external services. Across queries we run sequentially to avoid
    stampeding the same judge endpoints; for intra-set concurrency,
    batch ``candidates_by_query`` yourself and call this in chunks.

    Args:
        queries: The queries to judge. Iteration order is preserved in
            ``per_query``.
        candidates_by_query: Map of ``query_id`` → candidate synthesis
            text. Queries absent from this map are skipped.
        judges: The judge ensemble. Must be non-empty.

    Returns:
        The :class:`QualityReport`.

    Raises:
        ValueError: If ``judges`` is empty.
    """

    if len(judges) == 0:
        raise ValueError("requires at least one judge")

    per_query: list[QualityResult] = []
    total_judge_failures = 0

    for q in queries:
        candidate = candidates_by_query.get(q.query_id)
        if candidate is None:
            continue  # skip — nothing to score.

        args = JudgeCallArgs(
            query=q.query_text,
            candidate=candidate,
            # Forward the reference text only when the query carries one
            # (mirrors TS's `if (q.referenceSummary !== undefined)`).
            reference=q.reference_summary if q.reference_summary is not None else None,
        )

        per_judge_scores = await asyncio.gather(
            *(_call_one_judge(j, args) for j in judges),
        )

        total = 0.0
        count = 0
        for s in per_judge_scores:
            if s.score is None:
                total_judge_failures += 1
            else:
                total += s.score
                count += 1
        mean_score = total / count if count > 0 else None
        per_query.append(
            QualityResult(
                query_id=q.query_id,
                candidate=candidate,
                per_judge_scores=list(per_judge_scores),
                mean_score=mean_score,
            )
        )

    successful = [r for r in per_query if r.mean_score is not None]
    overall_mean = (
        sum(r.mean_score for r in successful if r.mean_score is not None) / len(successful)
        if len(successful) > 0
        else 0.0
    )

    return QualityReport(
        per_query=per_query,
        overall=QualityOverall(
            mean_score=overall_mean,
            n=len(successful),
            judge_failures=total_judge_failures,
        ),
    )
