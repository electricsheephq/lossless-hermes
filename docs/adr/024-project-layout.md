# ADR-024: Project layout

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

`lossless-claw` is organized under `src/` with one subsystem per directory (`src/db/`, `src/store/`, `src/embeddings/`, `src/tools/`, `src/voyage/`, `src/extraction/`, `src/synthesis/`, `src/eval/`, `src/operator/`, `src/plugin/`, `src/concurrency/`) plus a flat tail of top-level modules (`engine.ts`, `assembler.ts`, `compaction.ts`, `retrieval.ts`, `expansion.ts`, `large-files.ts`, `integrity.ts`, `prune.ts`, `transcript-repair.ts`, `transaction-mutex.ts`, etc.). The lcm-source-map (`docs/reference/lcm-source-map.md`) catalogs all 92 source files across 15 buckets totalling 48,847 LOC.

We must pick a Python package layout for `lossless-hermes` before writing `pyproject.toml`. The package will be pip-installed via entry point (ADR-001 — `[project.entry-points."hermes_agent.plugins"]`). The constraint forcing a choice: a 92-file 1:1 port is easier to review and operate when the source tree mirrors the target — every `git blame` lookup against `lossless-claw` should map to a deterministic path in `lossless-hermes`. Hermes's plugin convention is `src/<package_name>/` under a setuptools/hatchling source layout (`docs/reference/dependencies.md` "src layout").

## Options considered

### Option A: 1:1 mirror — `src/lossless_hermes/<subsystem>/`

- Description: Replicate `lossless-claw/src/` structure verbatim under `src/lossless_hermes/`. Subsystem directories: `db/`, `store/`, `tools/`, `embeddings/`, `extraction/`, `synthesis/`, `eval/`, `operator/`, `doctor/`, `plugin/`, `concurrency/`, `voyage/`. Top-level top-of-package modules: `engine.py` (split per ADR-027), `assembler.py`, `compaction.py`, `retrieval.py`, `expansion.py`, `large_files.py`, `integrity.py`, `prune.py`, `transcript_repair.py`, `transaction_mutex.py`, `types.py`, `log.py`, `estimate_tokens.py`, `session_patterns.py`, `hermes_bridge.py`.
- Pros:
  - Every TS file has a deterministic Python target documented in lcm-source-map (`docs/reference/lcm-source-map.md` "Complete file map").
  - Side-by-side review is mechanical: open `src/store/conversation-store.ts` and `src/lossless_hermes/store/conversation.py` in two panes.
  - Future LCM patch back-ports trivial — file-to-file mapping is unambiguous.
  - Hermes plugin convention (`src/<pkg>/` under hatchling) is satisfied without modification.
- Cons:
  - Inherits any quirks of LCM's organization (e.g. `doctor` files live under `plugin/` in TS but become a peer directory `doctor/` here — minor reshape).
  - The flat tail of top-level modules (~15 files) is less tidy than fully nested.
- Evidence cited:
  - `docs/reference/lcm-source-map.md` "Complete file map" — full 92-row table mapping every TS source to its Python target.
  - `docs/reference/lcm-source-map.md` "Bucket summary" — 15 named buckets, all preserved.
  - `docs/porting-guides/engine.md` — engine.ts is the only file that warrants further splitting (see ADR-027); the rest port 1:1.

### Option B: Layered architecture — group by abstraction level

- Description: Reorganize into `core/`, `storage/`, `services/`, `tools/`, `plugin/` with subsystems blended. E.g. `core/engine.py`, `core/assembler.py`, `core/compaction.py`; `services/embeddings.py`, `services/voyage.py`, `services/synthesis.py`.
- Pros: Cleaner taxonomy from a Python design standpoint.
- Cons:
  - Breaks the 1:1 mapping. Every back-port from LCM requires a manual cross-reference.
  - lcm-source-map.md has to be rewritten and kept in lockstep — high doc maintenance cost.
  - Reviewers can't open the two trees side-by-side. The cost compounds across 92 files and 1595 tests.
- Evidence: none cited in porting docs — pure design preference.

### Option C: Single flat module — one file per subsystem

- Description: One `.py` file per subsystem with no nested directories. `db.py`, `store.py`, `embeddings.py`, etc.
- Pros: Simplest layout.
- Cons:
  - LCM has 15+ files inside `store/` and 13+ inside `tools/`. Collapsing each into one `.py` produces 5000+ LOC files — same problem we are solving for `engine.ts` in ADR-027.
  - Loses the natural compose-by-import boundary inside each subsystem.

## Decision

Chosen: **Option A — 1:1 mirror under `src/lossless_hermes/`**.

Package tree:

```
src/lossless_hermes/
  __init__.py              # entry-point register() + 3 helper re-exports (lcm-source-map §index.ts)
  types.py                 # shared type contracts (TypedDict/Protocol/Pydantic mix)
  hermes_bridge.py         # ~30 LOC — replaces openclaw-bridge.ts (26 LOC); see ADR-024 §Hermes bridge below
  log.py                   # NOOP logger + describeLogError
  estimate_tokens.py       # code-point-aware token estimator
  session_patterns.py      # session-key glob compiler
  large_files.py           # <file> block extraction + file_<sha> ID handling
  integrity.py             # 8 integrity checks + repair plan
  prune.py                 # age-based conversation pruning
  transcript_repair.py     # tool_use ↔ tool_result pairing repair
  transaction_mutex.py     # per-DB async mutex
  assembler.py             # context-pyramid assembly
  compaction.py            # leaf/condensed creation + decision
  summarize.py             # LLM summarizer adapter
  retrieval.py             # grep + expand surface
  expansion.py             # sub-tree expansion w/ token cap
  expansion_policy.py      # intent → action router
  expansion_auth.py        # delegated-expansion grant manager
  engine/                  # split per ADR-027
    __init__.py            # LCMEngine class shell (state + lifecycle)
    ingest.py              # ingest_single + ingest_batch
    assemble.py            # assemble + safeFallback
    compact.py             # compact + executeCompactionCore
    lifecycle.py           # on_session_start/end/reset + maintain
  db/
    __init__.py
    connection.py
    features.py
    config.py
    migration.py
  store/
    __init__.py
    conversation.py
    summary.py
    compaction_telemetry.py
    compaction_maintenance.py
    conversation_scope.py
    fts5_sanitize.py
    full_text_sort.py
    full_text_fallback.py
    parse_utc_timestamp.py
    message_identity.py
  embeddings/
    __init__.py
    store.py
    backfill.py
    semantic_search.py
    hybrid_search.py
  voyage/
    __init__.py
    client.py
  extraction/
    __init__.py
    coreference.py
    llm_extractor.py
  synthesis/
    __init__.py
    dispatch.py
    prompt_registry.py
    seed_prompts.py
  tools/
    __init__.py
    common.py
    entity_shared.py
    grep.py
    describe.py
    expand.py
    expand_delegation.py
    expand_query.py
    synthesize_around.py
    get_entity.py
    search_entities.py
    compact.py
    conversation_scope.py
    expansion_recursion_guard.py
  eval/
    __init__.py
    run.py
    recall.py
    judge.py
    query_set.py
  operator/
    __init__.py
    purge.py
    health.py
    reconcile.py
    backfill_autostart.py
    extraction_autostart.py
    eval_runner.py
    semantic_infra.py
    worker_llm.py
    worker_orchestrator.py
  doctor/                  # peer dir (TS had this under plugin/)
    __init__.py
    apply.py
    cleaners.py
    shared.py
  plugin/                  # Hermes plugin registration + commands
    __init__.py            # entry-point register(ctx) per ADR-001
    commands.py            # /lcm slash-command handlers
    shared_init.py
    needs_compact_gate.py
    result_budget.py
    token_state.py
    db_backup.py
  concurrency/
    __init__.py
    worker_loop.py
    worker_lock.py
    model.py
```

## Hermes bridge

`src/lossless_hermes/hermes_bridge.py` (~30 LOC) replaces `src/openclaw-bridge.ts` (26 LOC, lcm-source-map §"Entry & top-level"). It is the seam between LCM internals and the Hermes plugin SDK: re-exports `PluginContext`, `ContextEngine`, hook-registration shapes, and any Hermes-side types LCM modules import. Centralizing the seam in one file means future Hermes ABC churn touches one file, not 50.

## Rationale

The 1:1 mirror is the layout lcm-source-map.md was authored for (`docs/reference/lcm-source-map.md` "Complete file map" lines 44–209) — it makes every "where does this go in Python?" question already answered in committed documentation. The cost of re-organizing is paid 92 times over (once per file) and again at every back-port; the cost of 1:1 is paid zero times because LCM already decided.

Layered (Option B) and flat (Option C) lose this contract. The porting effort is dominated by 1595 tests and ~40,000 LOC of behavior translation — saving doc-maintenance overhead by keeping the source map authoritative is the higher leverage.

The one structural change from LCM is promoting `doctor/` to a peer of `operator/` (rather than nesting it under `plugin/` as TS does). Three of the four doctor files are pure logic with no plugin-runtime dependency (`lcm-doctor-apply.ts`, `lcm-doctor-cleaners.ts`, `lcm-doctor-shared.ts` per lcm-source-map §"Doctor"); only `lcm-db-backup.ts` stays under `plugin/` because doctor uses it. This is recorded in the source map.

## Consequences

- **Every TS file has exactly one Python target** documented in `docs/reference/lcm-source-map.md`. Future contributors find paths by lookup, not invention.
- **`engine.ts` (8,731 LOC) splits per ADR-027** into `engine/__init__.py + ingest.py + assemble.py + compact.py + lifecycle.py`. All other files port to a single Python target.
- **`hermes_bridge.py` is the only file with no TS counterpart.** Drops `openclaw-bridge.ts`; adds the Hermes-side shim. Any new Hermes ABC contract lives here.
- **`startup-banner-log.ts` drops** per lcm-source-map "DROP list" (cosmetic, 54 LOC).
- **JSONL-bootstrap logic inside `engine.ts` drops** (~1,800 LOC of file-anchor/auto-rotate/transcript-repair-of-JSONL code; engine.md §"State owned by LcmContextEngine" lists the fields to drop). This is a per-method drop inside the engine split, not a file-level drop.
- **Invariant:** the `src/lossless_hermes/<subsystem>/<file>.py` layout matches the subsystem table in lcm-source-map. Renaming a subsystem requires updating the map first.
- **pyproject.toml** uses `[tool.hatch.build.targets.wheel] packages = ["src/lossless_hermes"]` and `[tool.hatch.build.targets.sdist] include = ["src/lossless_hermes"]`.

## Open questions / 5% uncertainty

1. **Subsystem `__init__.py` re-exports.** TS uses `src/store/index.ts` as a barrel. Python's idiomatic equivalent is to keep `__init__.py` minimal (re-exporting only the public surface) but make each module independently importable. Pick "minimal barrel" by default; revisit if call sites become noisy.
2. **`tools/conversation_scope.py` vs `store/conversation_scope.py` clash.** LCM has both (`src/tools/lcm-conversation-scope.ts` for session_key resolution; `src/store/conversation-scope.ts` for SQL fragment builder). Different files, same name. Python preserves the disambiguation by namespace (`tools.conversation_scope` vs `store.conversation_scope`). No collision because they live in different packages.
3. **Whether to flatten engine/ once it stabilizes.** ADR-027 keeps `engine/` as a directory for the v0.1 port. If the four sub-modules turn out to be unhelpful, a later ADR may collapse them. Not in scope here.
