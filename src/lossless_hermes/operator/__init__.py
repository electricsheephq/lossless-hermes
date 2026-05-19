"""Operator-facing surfaces — LCM v4.1 Wire-2.

Modules that wire LCM's internal jobs into operator-facing lifecycle hooks
(autostart, ``/lcm worker`` commands, ``/lcm health`` introspection). Issue
05-11 lands :mod:`lossless_hermes.operator.backfill_autostart`; issue 07-04
lands :mod:`lossless_hermes.operator.extraction_autostart`; issue 08-03
lands :mod:`lossless_hermes.operator.health`; Epic 08 fills in the rest
of the operator surface (purge, reconcile, eval-runner, worker-orchestrator,
etc.).

Ports the subset of ``lossless-claw/src/operator/*.ts`` that the
embedding backfill cron + entity-extraction cron + health introspection need.
"""

from lossless_hermes.operator.backfill_autostart import (
    DEFAULT_AUTOSTART_INTERVAL_S,
    AutostartHandle,
    AutostartLogger,
    start_embedding_backfill_autostart,
)
from lossless_hermes.operator.eval_runner import (
    EvalMode,
    EvalRunnerError,
    EvalRunnerErrorKind,
    RunEvalArgs,
    RunEvalResult,
    format_eval_report,
    run_eval,
)
from lossless_hermes.operator.extraction_autostart import (
    DEFAULT_EXTRACTION_INTERVAL_S,
    STARTUP_DELAY_S,
    ExtractionAutostartDeps,
    ExtractionAutostartHandle,
    ExtractionAutostartLogger,
    ExtractionTickFn,
    ExtractionTickResult,
    try_start_extraction_autostart,
)
from lossless_hermes.operator.health import (
    ActiveEmbeddingProfile,
    EmbeddingsHealth,
    EvalHealth,
    MostRecentEvalRun,
    SuppressionHealth,
    SynthesisHealth,
    V41HealthSnapshot,
    WorkerStatus,
    get_v41_health_snapshot,
)
from lossless_hermes.operator.reconcile import (
    ReconcileArgs,
    ReconcileCandidate,
    ReconcileError,
    ReconcileErrorKind,
    ReconcileResult,
    list_legacy_candidates,
    reconcile_session_keys,
)
from lossless_hermes.operator.semantic_infra import (
    DEFAULT_DIM,
    DEFAULT_MODEL,
    KNOWN_MODEL_DIMS,
    SemanticInfraDeps,
    SemanticInfraInitResult,
    init_semantic_infra_if_possible,
)
from lossless_hermes.operator.worker_orchestrator import (
    DEFAULT_WORKER_LLM_TIMEOUT_S,
    ExtractionTickArgs,
    ExtractionTickResultWithLock,
    ForceReleaseResult,
    HeartbeatResult,
    PendingCounts,
    WorkerLlmConfig,
    WorkerLockSnapshot,
    WorkerStatusSnapshot,
    create_worker_llm_call,
    force_release_lock,
    get_worker_status_snapshot,
    heartbeat_all_held_locks,
    tick_embedding_backfill,
    tick_extraction,
)

__all__ = [
    "ActiveEmbeddingProfile",
    "AutostartHandle",
    "AutostartLogger",
    "DEFAULT_AUTOSTART_INTERVAL_S",
    "DEFAULT_DIM",
    "DEFAULT_EXTRACTION_INTERVAL_S",
    "DEFAULT_MODEL",
    "DEFAULT_WORKER_LLM_TIMEOUT_S",
    "EmbeddingsHealth",
    "EvalHealth",
    "EvalMode",
    "EvalRunnerError",
    "EvalRunnerErrorKind",
    "ExtractionAutostartDeps",
    "ExtractionAutostartHandle",
    "ExtractionAutostartLogger",
    "ExtractionTickArgs",
    "ExtractionTickFn",
    "ExtractionTickResult",
    "ExtractionTickResultWithLock",
    "ForceReleaseResult",
    "HeartbeatResult",
    "KNOWN_MODEL_DIMS",
    "MostRecentEvalRun",
    "PendingCounts",
    "ReconcileArgs",
    "ReconcileCandidate",
    "ReconcileError",
    "ReconcileErrorKind",
    "ReconcileResult",
    "RunEvalArgs",
    "RunEvalResult",
    "STARTUP_DELAY_S",
    "SemanticInfraDeps",
    "SemanticInfraInitResult",
    "SuppressionHealth",
    "SynthesisHealth",
    "V41HealthSnapshot",
    "WorkerLlmConfig",
    "WorkerLockSnapshot",
    "WorkerStatus",
    "WorkerStatusSnapshot",
    "create_worker_llm_call",
    "force_release_lock",
    "format_eval_report",
    "get_v41_health_snapshot",
    "get_worker_status_snapshot",
    "heartbeat_all_held_locks",
    "init_semantic_infra_if_possible",
    "list_legacy_candidates",
    "reconcile_session_keys",
    "run_eval",
    "start_embedding_backfill_autostart",
    "tick_embedding_backfill",
    "tick_extraction",
    "try_start_extraction_autostart",
]
