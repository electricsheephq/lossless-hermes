"""Operator-facing surfaces — LCM v4.1 Wire-2.

Modules that wire LCM's internal jobs into operator-facing lifecycle hooks
(autostart, ``/lcm worker`` commands, ``/lcm health`` introspection). Issue
05-11 lands :mod:`lossless_hermes.operator.backfill_autostart`; Epic 08
fills in the rest of the operator surface (purge, reconcile, eval-runner,
worker-orchestrator, etc.).

Ports the subset of ``lossless-claw/src/operator/*.ts`` that the
embedding backfill cron needs.
"""

from lossless_hermes.operator.backfill_autostart import (
    DEFAULT_AUTOSTART_INTERVAL_S,
    AutostartHandle,
    AutostartLogger,
    start_embedding_backfill_autostart,
)

__all__ = [
    "DEFAULT_AUTOSTART_INTERVAL_S",
    "AutostartHandle",
    "AutostartLogger",
    "start_embedding_backfill_autostart",
]
