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

__all__ = [
    "ActiveEmbeddingProfile",
    "AutostartHandle",
    "AutostartLogger",
    "DEFAULT_AUTOSTART_INTERVAL_S",
    "DEFAULT_EXTRACTION_INTERVAL_S",
    "EmbeddingsHealth",
    "EvalHealth",
    "ExtractionAutostartDeps",
    "ExtractionAutostartHandle",
    "ExtractionAutostartLogger",
    "ExtractionTickFn",
    "ExtractionTickResult",
    "MostRecentEvalRun",
    "STARTUP_DELAY_S",
    "SuppressionHealth",
    "SynthesisHealth",
    "V41HealthSnapshot",
    "WorkerStatus",
    "get_v41_health_snapshot",
    "start_embedding_backfill_autostart",
    "try_start_extraction_autostart",
]
