"""Concurrency primitives for the lossless-hermes plugin.

Public API:

* :class:`WorkerLoop` — periodic background-job dispatcher (port of
  ``lossless-claw/src/concurrency/worker-loop.ts``).
* :class:`WorkerJob` — registration shape for a single job.
* :class:`JobCompleteInfo` — per-tick telemetry payload.
* :data:`WorkerJobKind` — ``Literal`` of the canonical job kinds tracked
  by the cross-process lock table.
* :data:`WORKER_JOB_KINDS` — runtime-iterable tuple of those kinds.
* Concurrency constants: :data:`GATEWAY_BUSY_TIMEOUT_MS`,
  :data:`WORKER_BUSY_TIMEOUT_MS`, :data:`WORKER_HEARTBEAT_MS`,
  :data:`WORKER_LOCK_TTL_MS`, :data:`GATEWAY_FALLBACK_SOAK_MS`.
* §0 invariant helpers: :func:`assert_no_open_tx`,
  :func:`assert_foreign_keys_enabled`.

See ``model.py`` and ``worker_loop.py`` module docstrings for the full
contract and pointers to ADR-018 / ADR-020.
"""

from __future__ import annotations

from lossless_hermes.concurrency.model import (
    GATEWAY_BUSY_TIMEOUT_MS,
    GATEWAY_FALLBACK_SOAK_MS,
    WORKER_BUSY_TIMEOUT_MS,
    WORKER_HEARTBEAT_MS,
    WORKER_JOB_KINDS,
    WORKER_LOCK_TTL_MS,
    WorkerJobKind,
    assert_foreign_keys_enabled,
    assert_no_open_tx,
)
from lossless_hermes.concurrency.worker_loop import (
    JobCompleteInfo,
    WorkerJob,
    WorkerLoop,
)

__all__ = [
    "GATEWAY_BUSY_TIMEOUT_MS",
    "GATEWAY_FALLBACK_SOAK_MS",
    "JobCompleteInfo",
    "WORKER_BUSY_TIMEOUT_MS",
    "WORKER_HEARTBEAT_MS",
    "WORKER_JOB_KINDS",
    "WORKER_LOCK_TTL_MS",
    "WorkerJob",
    "WorkerJobKind",
    "WorkerLoop",
    "assert_foreign_keys_enabled",
    "assert_no_open_tx",
]
