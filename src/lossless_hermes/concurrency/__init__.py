"""LCM concurrency primitives — cross-process worker-job coordination.

Ports ``lossless-claw/src/concurrency/`` (LCM commit ``1f07fbd``) to Python.

This package is the single source of truth for §0 of architecture-v4.1.md +
v4.1.1 A6/A9 amendments. Code that violates the §0 invariants (no LLM/network
inside a write transaction, gateway-owns-hot-path, etc.) must fail loudly
(assertion / raise) — never silently degrade.

### Public API

* :class:`WorkerLoop` — periodic background-job dispatcher (port of
  ``lossless-claw/src/concurrency/worker-loop.ts``) [PR #36 / issue 05-05].
* :class:`WorkerJob` — registration shape for a single job [#36].
* :class:`JobCompleteInfo` — per-tick telemetry payload [#36].
* :data:`WorkerJobKind` — ``Literal`` of the canonical job kinds tracked
  by the cross-process lock table.
* :data:`WORKER_JOB_KINDS` — runtime-iterable tuple of those kinds.
* Concurrency constants: :data:`GATEWAY_BUSY_TIMEOUT_MS`,
  :data:`WORKER_BUSY_TIMEOUT_MS`, :data:`WORKER_HEARTBEAT_MS`,
  :data:`WORKER_LOCK_TTL_MS`, :data:`GATEWAY_FALLBACK_SOAK_MS`, plus
  the seconds aliases :data:`WORKER_HEARTBEAT_S`, :data:`WORKER_LOCK_TTL_S`,
  :data:`GATEWAY_FALLBACK_SOAK_S` [#37].
* :class:`LockInfo`, :class:`LockOwner`, :class:`WorkerLockRow` —
  dataclasses for the worker-lock surface [#37 / issue 05-06].
* Worker-lock SQL primitives: :func:`acquire_lock`, :func:`heartbeat_lock`,
  :func:`release_lock`, :func:`lock_info`, :func:`generate_worker_id`,
  :func:`run_with_heartbeat` [#37].
* §0 invariant helpers: :func:`assert_no_open_tx` [#36],
  :func:`assert_foreign_keys_enabled` [#36/#37], :func:`assert_busy_timeout_for_role` [#37].

### Modules

* :mod:`lossless_hermes.concurrency.model` — load-bearing constants,
  job-kind literal, lock dataclasses, and §0 invariant runtime helpers.
* :mod:`lossless_hermes.concurrency.worker_loop` — periodic background-job
  dispatcher with generation-counter + skip-overlap semantics.
* :mod:`lossless_hermes.concurrency.worker_lock` — cross-process worker
  job lock backed by the ``lcm_worker_lock`` table, ports the four SQL
  primitives plus the :func:`run_with_heartbeat` async wrapper.

See ``docs/adr/018-concurrency-model.md`` and ``docs/adr/020-async-worker-task.md``.
"""

from __future__ import annotations

from lossless_hermes.concurrency.model import (
    GATEWAY_BUSY_TIMEOUT_MS,
    GATEWAY_FALLBACK_SOAK_MS,
    GATEWAY_FALLBACK_SOAK_S,
    WORKER_BUSY_TIMEOUT_MS,
    WORKER_HEARTBEAT_MS,
    WORKER_HEARTBEAT_S,
    WORKER_JOB_KINDS,
    WORKER_LOCK_TTL_MS,
    WORKER_LOCK_TTL_S,
    LockInfo,
    LockOwner,
    WorkerJobKind,
    WorkerLockRow,
    assert_busy_timeout_for_role,
    assert_foreign_keys_enabled,
    assert_no_open_tx,
)
from lossless_hermes.concurrency.worker_lock import (
    acquire_lock,
    generate_worker_id,
    heartbeat_lock,
    lock_info,
    release_lock,
    run_with_heartbeat,
)
from lossless_hermes.concurrency.worker_loop import (
    JobCompleteInfo,
    WorkerJob,
    WorkerLoop,
)

__all__ = [
    "GATEWAY_BUSY_TIMEOUT_MS",
    "GATEWAY_FALLBACK_SOAK_MS",
    "GATEWAY_FALLBACK_SOAK_S",
    "JobCompleteInfo",
    "LockInfo",
    "LockOwner",
    "WORKER_BUSY_TIMEOUT_MS",
    "WORKER_HEARTBEAT_MS",
    "WORKER_HEARTBEAT_S",
    "WORKER_JOB_KINDS",
    "WORKER_LOCK_TTL_MS",
    "WORKER_LOCK_TTL_S",
    "WorkerJob",
    "WorkerJobKind",
    "WorkerLockRow",
    "WorkerLoop",
    "acquire_lock",
    "assert_busy_timeout_for_role",
    "assert_foreign_keys_enabled",
    "assert_no_open_tx",
    "generate_worker_id",
    "heartbeat_lock",
    "lock_info",
    "release_lock",
    "run_with_heartbeat",
]
