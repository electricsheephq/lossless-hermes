# ADR-010: Always-on assembly substitution mechanism

**Status:** Superseded by ADR-032
**Date:** 2026-05-13
**Confidence:** 60% (because outcome hinges on upstream maintainer acceptance)
**Supersedes:** —
**Superseded by:** ADR-032

## Context

LCM's load-bearing architectural property is **always-on assembly substitution**: every turn, BEFORE the LLM sees the message list, LCM rewrites it from the DAG (`contextItems` + summaries) under a token budget. This is the mechanism by which evicted raw turns are replaced with summary stubs and recovered context-items are spliced in. It is structurally different from OpenClaw / Hermes overflow-only compaction, which only fires when prompt tokens cross a threshold.

In OpenClaw, the host called `engine.assemble({ messages, tokenBudget, ... })` BEFORE every turn (TS `src/engine.ts:6648–6832`). Hermes has no such call. Spike 002 (`docs/spike-results/002-hermes-pre-llm-call.md`) catalogued every Hermes hook with `messages`-replacement semantics; the conclusion (line 51): **NO via `pre_llm_call`. The hook is structurally additive.**

Spike 002 confirmed:

- `pre_llm_call` (`run_agent.py:12034`) captures plugin return values, joins them with `\n\n`, and APPENDS the joined string to the current turn's user-message content at API-call time (`run_agent.py:12266–12277`). The `conversation_history` kwarg is `list(messages)` — a shallow copy. Mutating it in-place does not affect the live messages list.
- The only mechanism in Hermes that REPLACES the message list is `ContextEngine.compress(messages, ...)` — its return value REPLACES `messages` at `run_agent.py:10264`. But this only fires when `should_compress()` returns True (`run_agent.py:14841`) — a threshold gate, not an every-turn signal.

The constraint forcing a choice: LCM must substitute every turn, but Hermes provides no per-turn substitution hook out-of-the-box.

## Options considered

### Option A: Force `should_compress() → True` every turn (experimental fallback)

- **Description:** Set `should_compress` to always return True and put LCM's assembly inside `compress(messages, ...)`. Every turn, `compress()` fires and replaces the message list with the LCM-assembled version.
- **Pros:** Works mechanically with zero upstream changes. Validates the LCM algorithm in isolation.
- **Cons:** Catastrophic side effects per spike 002 lines 127–137:
  - **Session-ID rotation every turn** — `run_agent.py:10311–10337` ends the current SQLite session and starts a new one inside every `_compress_context` invocation. Gateway routing, memory provider lineage, langfuse traces, and `parent_session_id` chains all break.
  - **`commit_memory_session(messages)` fires every turn** — memory providers re-extract from the same conversation N times.
  - **Compression-count warning trips in 2–3 turns** — "Session compressed N times — accuracy may degrade. Consider /new to start fresh."
  - **File-read dedup cache reset every turn** — model re-reads files it just read.
  - **Log spam** on every turn (`logger.info("context compression done…")`).
  - **No control over preflight ordering** — preflight uses raw `_preflight_tokens >= threshold_tokens` (`run_agent.py:11971`), forcing it to fire every turn requires `threshold_tokens=0`, which violates other invariants.
- **Evidence:** Spike 002 "Option A" section (lines 125–137). Spike 002 verdict: "**NOT shippable** — it breaks session lifecycle, memory provider lineage, and observability." Acceptable only as experimental validation in isolation.

### Option B: Upstream ABC patch — `ContextEngine.preassemble(messages, budget) → messages`

- **Description:** Open a Hermes upstream PR adding `preassemble(messages, budget_tokens=None) → List[Dict]` to `ContextEngine` ABC. Add ONE call site in `run_agent.py` around line 12018 (after preflight compression, before `pre_llm_call`):

  ```python
  if hasattr(self.context_compressor, "preassemble"):
      messages = self.context_compressor.preassemble(messages, budget_tokens=...)
  ```

- **Pros:** ~30 LOC upstream change (one ABC method + one call site + one test). Default returns input unchanged → non-breaking for all existing engines. Clean shippable extension point. Does not conflict with `compress()` semantics (compress = overflow recovery, preassemble = every-turn substitution). Benefits any future ContextEngine plugin, not just LCM.
- **Cons:** Depends on upstream Hermes maintainer accepting the PR. Until merged, lossless-hermes cannot ship a production-grade always-on assembly.
- **Evidence:** Spike 002 "Option B" section (lines 139–166). Spike 002 verdict: "**Cleanest path.** Strongly preferred." See ADR-015 patch #1 for the upstream PR plan.

### Option C: Preflight choke point (`run_agent.py:11958–12017`) with `threshold_tokens=0`

- **Description:** Set `threshold_tokens=0` so the preflight compression loop fires every turn and routes through `_compress_context`.
- **Pros:** Avoids the post-response check at `run_agent.py:14841`.
- **Cons:** Preflight ALSO calls `_compress_context`, which still rotates the session — **same root problem as Option A**.
- **Evidence:** Spike 002 line 170–174. Verdict: "Worse than A (less idiomatic), same trade-offs. Skip."

### Option D: Hijack `should_compress_preflight`

- **Description:** Override `should_compress_preflight(messages) → True`.
- **Pros:** N/A.
- **Cons:** **Dead code in current Hermes** — `should_compress_preflight` is declared at `agent/context_engine.py:100` but `grep` returns zero call sites in `run_agent.py` (spike 002 line 180, `docs/reference/hermes-hooks.md:53`). Overriding it has no effect.
- **Evidence:** Spike 002 lines 176–180.

## Decision

Chosen: **Option B (upstream PR for `preassemble`), with Option A as documented experimental fallback.**

Pursue the Hermes upstream PR adding `ContextEngine.preassemble(messages, budget) → messages` (default no-op). The PR is captured as patch #1 in ADR-015.

While the PR is pending, lossless-hermes ships in **experimental mode** with Option A (force compress every turn) enabled behind a `plugins.entries.lossless-hermes.experimental.always_on_via_compress: true` config flag. The README and release notes must explicitly document:

- Session-ID rotates every turn
- Memory provider lineage breaks
- Compression-count warnings will fire
- This is **not shippable for production** — it is an isolation-validation mode

Once the upstream PR merges, the experimental flag is removed and Option B becomes the only path.

## Rationale

Spike 002 explicitly identifies `pre_llm_call` as append-only — it cannot rewrite messages. The only mechanism that REPLACES the message list is `compress()`, but its threshold-gated invocation rotates the session as a side effect. There is no alternative in-tree hook that accomplishes substitution without the rotation hazard.

The upstream PR is small (~30 LOC), additive, non-breaking (default returns input unchanged), and benefits the entire ContextEngine ABC contract — making it a cleanup any engine author would benefit from. Spike 002 recommendation (line 184): "Pursue Option B. Open an upstream PR to Hermes adding `ContextEngine.preassemble(messages, budget) -> messages` (default no-op)."

The fallback "force compress" is acceptable for short-term validation because:

- It proves the LCM assembly algorithm against real Hermes runtimes (the algorithm is the hard part; the integration seam is mechanical).
- It surfaces real-world cache and latency interactions early.
- Its costs are operational (log spam, lineage corruption) not correctness (assembled messages ARE replaced) — meaning the validation results are still informative.

## Consequences

- **Cannot ship a stable v1.0 without the upstream PR.** Status of this ADR remains Proposed until the PR merges.
- **The 30-LOC patch is captured in ADR-015 (patch #1).** Code is written; PR can be filed immediately.
- **Experimental fallback adds a `plugins.entries.lossless-hermes.experimental.*` config namespace.** Document clearly that this is not for production.
- **The `compress()` ABC method remains the OVERFLOW-recovery path.** LCM-on-Hermes splits assembly (every turn, via `preassemble`) from compaction (overflow, via `compress`) — both routes share the same DAG-store backend.
- **One upstream review cycle blocks v1.0.** Mitigation: file the PR early in Phase 2; in parallel, ship experimental fallback so internal validation can proceed.

## Open questions / 5% uncertainty

- **Will the Hermes maintainer accept the PR?** Risk: low-to-medium. The patch is non-breaking, additive, and benefits any context engine. But maintainers can reject for taste/strategy reasons. Mitigation: pre-socialize the PR with a design note before filing; align on the API shape (`preassemble` vs `transform_messages` vs `assemble_messages`).
- **PR review latency.** Even if accepted, the merge may take weeks. Mitigation: ship experimental fallback so the rest of the port unblocks.
- **The `budget_tokens` parameter shape.** Spike 002 proposed `budget_tokens: int = None`. Hermes maintainer may prefer passing the entire `LcmConfig`-style budget object or computing budget on the engine side. Decide during PR review; both work for LCM.
- **Empirical cache-hit-rate measurement.** Engine.md (`docs/porting-guides/engine.md:559–562`) notes always-on assembly may impact prompt cache. Spike 002 confirmed `pre_llm_call` injection preserves cache. `preassemble` rewrites the entire message list — the cache impact for `preassemble` is unmeasured. Run cache-hit benchmark in Phase 2 once the patch lands.
