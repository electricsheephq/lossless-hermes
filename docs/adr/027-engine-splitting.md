# ADR-027: Engine.py splitting

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 90%
**Supersedes:** —
**Superseded by:** —

## Context

`lossless-claw/src/engine.ts` is **8,731 LOC in a single file** (verified by `wc -l` on `pr-613` per `docs/reference/lcm-source-map.md` line 6). The file is roughly 350 lines of module-private helpers followed by one ~6,950-line `LcmContextEngine` class declaration (`docs/porting-guides/engine.md` line 14).

The Python target (per ADR-024 §"Project layout") is estimated at ~6,800 LOC after dropping JSONL bootstrap / auto-rotate / file-anchor logic (engine.md "What changes — DROP" lists ~1,900 LOC of methods to remove). That is still too large for a single Python file:

- Code review at 6,800 LOC is impractical; reviewers cannot hold the whole file in working memory.
- Test isolation is harder — `engine.test.ts` itself is 11,922 LOC across 228 tests (per `docs/porting-guides/tests-and-config.md` line 45).
- Future patches that touch one cluster (e.g. ingest) risk silently affecting another (e.g. compact) when everything lives in one file.

lcm-source-map.md flags this directly (`docs/reference/lcm-source-map.md` line 239):

> **`engine.ts` is 8,731 LOC in one file.** Single-file port is risky; consider splitting the Python target into `engine/{__init__.py, ingest.py, assemble.py, compact.py, bootstrap.py}` along the natural method-cluster boundaries already present (search for the section banners `// ── …` in `engine.ts`).

The constraint forcing a choice: how do we split a single class into multiple files without losing the per-session-queue invariants or fragmenting the `LCMEngine` mental model?

## Options considered

### Option A: Single file (no split)

- Description: Keep `src/lossless_hermes/engine.py` as one ~6,800 LOC file mirroring `engine.ts`.
- Pros:
  - Maximum 1:1 mapping fidelity with TS source.
  - No mixin/import ceremony.
- Cons:
  - 6,800 LOC review unit. Hard to read, hard to bisect bugs.
  - One file becomes a single point of merge contention — every parallel port stream touching the engine collides here.
  - Diff-readability suffers; reviewers default to "looks correct" on long diffs.

### Option B: Split into sub-modules along section-banner boundaries (mixin pattern)

- Description: `src/lossless_hermes/engine/` package. `__init__.py` defines `LCMEngine` shell + state + ABC method signatures. `ingest.py`, `assemble.py`, `compact.py`, `lifecycle.py` define mixin classes containing the methods clustered by responsibility. `LCMEngine` inherits from all four mixins. The single class still owns the methods; the sub-modules are organizational.
- Pros:
  - Each sub-module is 1,000–2,000 LOC — review-able.
  - Mirrors the natural method clusters already in `engine.ts` (the `// ── ` section banners delimit these).
  - One class, one mental model — callers see `engine.ingest(...)` exactly as before; no factor-out costs.
  - Per-session queue (`_session_locks`) is owned by the shell class; mixin methods acquire via `self._session_locks[session_id]` — no fragmentation of the invariant.
- Cons:
  - Python mixin pattern requires careful MRO. Multiple inheritance with state-touching methods can be subtle.
  - Sub-module imports must be cyclic-free (each mixin imports `from .types import ...`, never from sibling mixins).

### Option C: Split into sub-modules with module-level functions

- Description: Same file split as B, but each sub-module exposes top-level `async def` functions that take an `engine` parameter. `LCMEngine.ingest(self, ...)` is a thin wrapper calling `await ingest_module.ingest(self, ...)`.
- Pros:
  - No multiple inheritance; cleanest mental model from a function-programming perspective.
- Cons:
  - The class becomes a "data + thin-wrapper" shell. Every method is two hops (caller → engine.method → module.function).
  - Function signatures grow (each function takes `engine` plus original args).
  - Test boundary is now per-function rather than per-method; tests written against TS's `engine.ingest()` shape need refactoring.

### Option D: Aggressive split (one file per major method)

- Description: `engine/ingest.py`, `engine/ingest_batch.py`, `engine/assemble.py`, `engine/safe_fallback.py`, `engine/compact.py`, `engine/compact_until_under.py`, `engine/evaluate_incremental_compaction.py`, `engine/deferred_debt_drain.py`, ... — 15+ files.
- Pros: Each file is small.
- Cons: Method co-location is broken. `evaluate_incremental_compaction` is the input to `compact`; splitting them across files makes co-evolution painful. Over-fragmentation.

## Decision

Chosen: **Option B — sub-modules organized by responsibility cluster, joined via mixins**.

Package structure:

```
src/lossless_hermes/engine/
  __init__.py        # ~600 LOC: LCMEngine class shell, state, ABC method signatures,
                     # ContextEngine inheritance, mixin composition, helpers (token estimation,
                     # hash utilities, circuit-breaker primitives)
  ingest.py          # ~1,400 LOC: ingest_single, ingest_batch, media-interception
                     # pipeline integration. Maps to engine.ts §"ingest" cluster + media seam.
  assemble.py        # ~1,200 LOC: assemble, safe_fallback, prefix-stability snapshotting,
                     # deferred-debt consumption during assemble. Maps to engine.ts §"assemble".
  compact.py         # ~2,000 LOC: compact, execute_compaction_core, compact_until_under,
                     # evaluate_incremental_compaction (~180 LOC state machine),
                     # cache-aware deferral gates, circuit-breaker integration,
                     # deferred-debt record + drain. Maps to engine.ts §"compact" cluster.
  lifecycle.py       # ~600 LOC: on_session_start, on_session_end, on_session_reset,
                     # handle_before_reset, handle_session_end, maintain-equivalent
                     # background drain task. Maps to engine.ts §"bootstrap" + §"maintain"
                     # (heavily simplified — JSONL paths drop).
```

The `LCMEngine` class composes the mixins:

```python
# src/lossless_hermes/engine/__init__.py
from agent.context_engine import ContextEngine
from .ingest import _IngestMixin
from .assemble import _AssembleMixin
from .compact import _CompactMixin
from .lifecycle import _LifecycleMixin


class LCMEngine(_LifecycleMixin, _CompactMixin, _AssembleMixin, _IngestMixin, ContextEngine):
    """Lossless Context Management engine for Hermes.

    Mixin order (MRO): _LifecycleMixin -> _CompactMixin -> _AssembleMixin -> _IngestMixin -> ContextEngine.
    Each mixin owns a responsibility cluster (see ADR-027). State (DB, stores, session locks,
    last-seen-message-idx, circuit-breaker states, telemetry caches) lives on the class shell here.
    """

    name = "lcm"
    threshold_percent = 0.75

    def __init__(self, hermes_home, config=None, summarizer=None):
        super().__init__()
        # ... state initialization (see engine.md §"Python class skeleton" line 363+)
```

Each mixin is a `private` class (underscore-prefixed) — not instantiated directly. Mixin methods exclusively read/write `self.<state>` declared on the shell class. No mixin imports from another mixin.

## Rationale

`engine.ts` already has natural cluster boundaries: the `// ── ` section banners delimit ingest / assemble / compact / lifecycle method groups. lcm-source-map.md (`docs/reference/lcm-source-map.md` line 239) explicitly recommends splitting along these. Engine.md confirms the structure supports clean split (`docs/porting-guides/engine.md` "Port order within this file" lines 498–510 — the 11-step port order maps to the same clusters).

Mixin pattern (Option B) over module-level functions (Option C) because:

- The TS source is one class; tests target `engine.ingest(...)`. Keeping the call shape preserves the test port surface.
- The per-session queue invariant (`async with self._session_locks[session_id]:`) is acquired by mixin methods on `self`. One class, one lock dict, one invariant.
- Multiple inheritance is a known Python pattern (CPython itself uses it heavily — `collections.abc`). MRO with single-shell-state ownership is well-understood.

Aggressive split (Option D) was rejected because `evaluate_incremental_compaction` and `compact` co-evolve; splitting them across files inflates merge friction and obscures the decision-logic-to-action flow.

Single file (Option A) was rejected because 6,800 LOC is below the LCM TS size only because of the JSONL drops — it's still ~1.5× the next-largest file in the Python target (`db/migration.py` at ~2,200 LOC). Splitting the largest file is the highest-leverage organizational decision in the port.

## Consequences

- **Single `LCMEngine` class still owns the methods.** Callers see no difference: `engine.ingest(msg)`, `engine.compress(messages, ...)`, `engine.on_session_start(session_id)` all work as before.
- **Mixin classes are underscore-prefixed and not exported.** Only `LCMEngine` itself is publicly importable from `lossless_hermes.engine`.
- **All state lives on the shell class.** Mixin methods touch `self._session_locks`, `self._last_seen_message_idx`, `self._circuit_breaker_states`, `self.conversation_store`, etc. — all initialized in `LCMEngine.__init__`. No mixin owns state.
- **No cross-mixin imports.** If `_CompactMixin` needs to call `_IngestMixin` behavior, it does so via `self.ingest_batch(...)` — which resolves through MRO to `_IngestMixin.ingest_batch`. No `from .ingest import ...` in `compact.py`.
- **Method docstrings cite the TS line range** they correspond to (e.g. `"""Maps to engine.ts:5899–6064 (ingestSingle private body)."""`) so back-ports and bug-hunts stay easy.
- **`engine.test.ts` (228 tests) ports to `tests/test_engine.py`** as a single file — the test file is organized by method, not by cluster, so the split does not propagate to tests.
- **`docs/reference/lcm-source-map.md` updates the engine row** to point to the directory:
  `src/engine.ts` (8731) → `src/lossless_hermes/engine/` (~6,800 LOC across 5 files).
- **Invariant:** the public `LCMEngine` surface (ABC methods + LCM-specific public methods listed in engine.md §"Auxiliary public methods") MUST remain a single class. No method may be a free function. This preserves test-port and call-site fidelity.
- **Future-proof:** if a sub-module grows past ~2,500 LOC, split it further (e.g. `compact.py` → `compact/__init__.py + compact/decision.py + compact/drain.py`). Document the split in a follow-up ADR.

## Open questions / 5% uncertainty

1. **MRO surprises with `ContextEngine` ABC.** If a Hermes ABC method (e.g. `should_compress`) is overridden by both `_CompactMixin` and the shell class, Python's C3 linearization picks the shell class first (since it's leftmost in the bases list except the mixins, which are explicitly ordered before ABC). The convention is: ABC overrides live in the shell class only; mixins define private helpers. Enforced by code review + a unit test asserting MRO order.
2. **Test discoverability.** `pytest` discovers `tests/test_engine.py` regardless of how the source splits; no change. But if a contributor adds new engine state to a mixin instead of the shell, tests that set up `LCMEngine` directly won't see the new state. Mitigation: explicit `__init__` initialization on the shell class is the only state-creation site.
3. **Diff readability across refactor boundaries.** A future PR that moves a method from `_CompactMixin` to `_IngestMixin` (or similar) shows up in `git diff` as a delete-then-add, not a rename. `git log --follow` works at file granularity, not method granularity. Mitigation: such reorgs warrant their own ADR with a rationale.
4. **Could decompose further (e.g. media interception as its own file).** The media-interception pipeline (engine.md §"Media interception pipeline" lines 265–269) is ~900 LOC of leaf utility. Engine.md recommends porting to `lossless_hermes/media.py` as a separate module. ADR-024 leaves `large_files.py` at the package root — re-evaluate placing media interception there during the port. Not blocking this ADR.
