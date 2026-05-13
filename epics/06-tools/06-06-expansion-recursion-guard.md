---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm-expansion-recursion-guard.ts'
labels: 'port'
---

## Source (TypeScript)
- File: `src/tools/lcm-expansion-recursion-guard.ts`
- Lines: ~373 LOC
- Function(s)/class(es): `createExpansionRequestId`, `resolveExpansionRequestId`, `resolveNextExpansionDepth`, `stampDelegatedExpansionContext`, `clearDelegatedExpansionContext`, `evaluateExpansionRecursionGuard`, `acquireExpansionConcurrencySlot`, `releaseExpansionConcurrencySlot`, `recordExpansionDelegationTelemetry`, `EXPANSION_DELEGATION_DEPTH_CAP = 1`.

## Target (Python)
- File: `src/lossless_hermes/tools/expansion_recursion_guard.py`
- Estimated LOC: ~350 LOC.

## Dependencies
- Depends on: none (pure in-memory state).
- Blocks: `lcm_expand_query` (deferred to v2 per [ADR-012](../../docs/adr/012-subagent-defer.md)) — but the module is still required as a shared infrastructure piece because `lcm_expand` queries the depth state via `resolveNextExpansionDepth` even when invoked directly.

## Acceptance criteria
- [ ] **In-memory state, three maps** at module level (TS uses module-level closures; Python uses a dataclass or module-level dicts protected by `threading.Lock`):
  - `_delegated_context_by_session_key: dict[str, DelegatedExpansionContext]`
  - `_blocked_request_ids_by_session_key: dict[str, set[str]]`
  - `_active_request_id_by_origin_session_key: dict[str, str]`
- [ ] `threading.Lock` (or `asyncio.Lock` if the call site is async — TS is single-threaded but Python may face concurrent dispatch from asyncio or threads). Pick `threading.Lock` per [ADR-017](../../docs/adr/017-sync-vs-async-db.md) (sync surface).
- [ ] All 9 exported functions ported with matching signatures:
  - `create_expansion_request_id() -> str` (uses `uuid.uuid4().hex` or similar).
  - `resolve_expansion_request_id(session_key: str) -> str | None` (inherits from stamped context).
  - `resolve_next_expansion_depth(session_key: str) -> int` — returns 1 by default, or `stamped + 1`.
  - `stamp_delegated_expansion_context(*, session_key, request_id, expansion_depth, origin_session_key, stamped_by)`.
  - `clear_delegated_expansion_context(session_key: str) -> None`.
  - `evaluate_expansion_recursion_guard(*, session_key, request_id) -> RecursionDecision` — blocks at depth >= 1 with `depth_cap` or `idempotent_reentry` reason.
  - `acquire_expansion_concurrency_slot(*, origin_session_key, request_id) -> SlotResult`.
  - `release_expansion_concurrency_slot(*, origin_session_key, request_id) -> None`.
  - `record_expansion_delegation_telemetry(*, deps, component, event, **kwargs)` — logs through `logging.getLogger("lcm.expansion")` with monotonic counters per tools.md line 578.
- [ ] `EXPANSION_DELEGATION_DEPTH_CAP = 1` — hard-coded module constant.
- [ ] `reset_for_tests()` helper that clears all three maps + the telemetry counters. Used by pytest fixtures.
- [ ] Telemetry counters: monotonic, in-memory; reset only via `reset_for_tests()`.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- `tests/tools/test_expansion_recursion_guard.py`:
  - Fresh state: `resolve_next_expansion_depth("foo")` → 1.
  - After stamp at depth 1: `resolve_next_expansion_depth("foo")` → 2; `evaluate_expansion_recursion_guard` returns `{blocked: True, reason: "depth_cap"}`.
  - Idempotent re-entry: same `request_id` against same session → `{blocked: True, reason: "idempotent_reentry"}`.
  - Concurrency slot: acquire returns ok; second concurrent acquire from same origin → blocked.
  - Telemetry counter: each event call increments the corresponding counter; `reset_for_tests` zeros it.
  - **Concurrency stress test:** 50 threads acquire the same origin slot in parallel; exactly 1 wins, 49 are blocked.

## Estimated effort
**5 hours** — port is mechanical (~2h); the concurrency test + threading lock semantics adds 3h.

## Confidence
**90%** — the TS module is single-threaded; Python's threading model means we need the lock + a concurrency stress test that doesn't exist in TS. 10% risk on getting the lock granularity right (per-function vs per-state-mutation).

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) lines 561–580.
- [ADR-012](../../docs/adr/012-subagent-defer.md) — `lcm_expand_query` is deferred but this guard module still ports because `lcm_expand` uses `resolve_next_expansion_depth`.
