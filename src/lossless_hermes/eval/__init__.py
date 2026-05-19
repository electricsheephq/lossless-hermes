"""Eval harness — LCM v4.1 §11 / D.03.

Ports the subset of ``lossless-claw/src/eval/`` needed by the
``/lcm eval`` operator command:

* :mod:`lossless_hermes.eval.query_set` — query-set registration +
  lookup against ``lcm_eval_query_set`` / ``lcm_eval_query``.
* :mod:`lossless_hermes.eval.recall` — pure recall@K metric module
  with caller-injected retrieval adapter.
* :mod:`lossless_hermes.eval.judge` — LLM-as-judge ensemble harness
  (``run_quality_eval``) with caller-injected judges and per-judge
  failure tolerance.
* :mod:`lossless_hermes.eval.run` — eval-run recording into
  ``lcm_eval_run`` + drift comparison against the most-recent prior
  run.

The operator-facing entry point is
:func:`lossless_hermes.operator.eval_runner.run_eval` which composes
all three. Tests use a mock :class:`~lossless_hermes.eval.recall.RecallSearchAdapter`
so neither Voyage nor sqlite-vec are required.

Ports ``lossless-claw/src/eval/`` (LCM commit ``1f07fbd`` on branch
``pr-613``):

* ``src/eval/query-set.ts`` — 292 LOC.
* ``src/eval/recall.ts`` — 237 LOC.
* ``src/eval/judge.ts`` — 191 LOC.
* ``src/eval/run.ts`` — 376 LOC.

The :mod:`~lossless_hermes.eval.judge` synthesis-quality module ships
its own caller-injected judge harness — it carries **no LLM wiring**;
the wiring is a Group F concern that reuses the synthesis llm-adapter
(``#09-07`` / ``#09-08`` callers).
"""

from lossless_hermes.eval.judge import (
    JudgeCall,
    JudgeCallArgs,
    JudgeCallResult,
    JudgeEntry,
    PerJudgeScore,
    QualityOverall,
    QualityReport,
    QualityResult,
    run_quality_eval,
)
from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySet,
    QuerySetIdentity,
    Stratum,
    decode_query_set_id,
    encode_query_set_id,
    get_query_set,
    list_query_sets,
    register_query_set,
)
from lossless_hermes.eval.recall import (
    DEFAULT_K_VALUES,
    RecallEvalOptions,
    RecallReport,
    RecallResult,
    RecallSearchAdapter,
    RecallStratumAggregate,
    run_recall_eval,
)
from lossless_hermes.eval.run import (
    DriftDetail,
    DriftSummary,
    EvalRunRecord,
    EvalTrigger,
    compute_drift,
    record_eval_run,
)

__all__ = [
    "DEFAULT_K_VALUES",
    "DriftDetail",
    "DriftSummary",
    "EvalRunRecord",
    "EvalTrigger",
    "JudgeCall",
    "JudgeCallArgs",
    "JudgeCallResult",
    "JudgeEntry",
    "PerJudgeScore",
    "QualityOverall",
    "QualityReport",
    "QualityResult",
    "QueryRecord",
    "QuerySet",
    "QuerySetIdentity",
    "RecallEvalOptions",
    "RecallReport",
    "RecallResult",
    "RecallSearchAdapter",
    "RecallStratumAggregate",
    "Stratum",
    "compute_drift",
    "decode_query_set_id",
    "encode_query_set_id",
    "get_query_set",
    "list_query_sets",
    "record_eval_run",
    "register_query_set",
    "run_quality_eval",
    "run_recall_eval",
]
