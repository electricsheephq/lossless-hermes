# ADR-015: Hermes-side upstream patches to propose

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 80%
**Supersedes:** —
**Superseded by:** —

## Context

The lossless-hermes port shipped through ADRs 009–014 picks workarounds at every Hermes-integration seam where the host doesn't quite fit LCM's needs:

- ADR-009 chose `post_llm_call` + `handle_tool_call` diff instead of a dedicated `ingest()` hook.
- ADR-010 chose to force-compress every turn (experimental fallback) instead of a clean per-turn assembly hook.
- ADR-014 chose user-message injection instead of system-prompt injection (correct under the Hermes invariant, but only because of cache-prefix immutability).
- ADR-009 alluded to `pre_llm_call` not firing on interrupted turns.

Each of these is a workaround. They function, but they're not clean. The port can ship without any upstream Hermes changes — every workaround is real and tested.

However: a small set of additive ABC patches would make LCM-on-Hermes meaningfully more robust AND benefit any future ContextEngine plugin. Total upstream change: ~100 LOC across 3-4 patches. All additive. All non-breaking. All have graceful-degrade-without-them.

The constraint forcing a choice: do we propose upstream patches, ship workaround-only, or both?

## Options considered

### Option A: Don't propose any patches

- **Description:** Ship lossless-hermes against vanilla Hermes. All workarounds in ADRs 009–014 are the long-term path.
- **Pros:** No upstream coordination cost. No risk of rejection. v1 ships when port-internal work is done.
- **Cons:** Workarounds are correct but suboptimal. Every future ContextEngine plugin will have to invent the same workarounds. LCM users get slightly higher latency / lower cache hits / occasional missed turns than the design admits in principle.
- **Evidence:** Implicit — all of ADRs 009–014 work without patches.

### Option B: Propose all useful patches upstream

- **Description:** File the small upstream PRs in parallel with port development. None blocks v1. v1 ships with workarounds; subsequent releases pick up patches as they merge.
- **Pros:** Makes the whole ContextEngine surface cleaner. Other plugin authors benefit. LCM becomes the canonical "ContextEngine done right" reference. Low effort (~100 LOC total).
- **Cons:** Upstream review takes time. PRs may be rejected for taste/strategy. Maintainers may push back on patch surface area.
- **Evidence:** Spike 002 line 184 ("Pursue Option B. Open an upstream PR to Hermes adding `ContextEngine.preassemble(messages, budget) -> messages`"); engine.md ADR-04, ADR-05, ADR-06 all flag potential upstream patches.

### Option C: Propose only the highest-value patch (preassemble)

- **Description:** Only file the `preassemble` PR. Keep the other workarounds as documented limitations.
- **Pros:** Smallest upstream coordination cost. The single most-impactful patch (always-on assembly) gets attention.
- **Cons:** Leaves three additional patches (each cheaper, each smaller) on the table. Future plugins still pay the workaround cost.
- **Evidence:** This is the de-facto minimum if Option B doesn't pan out.

## Decision

Chosen: **Option B — propose 3-4 additive ABC patches upstream**

None are blocking — every gap has a working workaround. The patches are filed in parallel with port work. v1 of lossless-hermes ships with workarounds; the patches integrate as they merge.

## Patches to propose

### Patch #1: `ContextEngine.preassemble(messages, budget) → messages`

- **What:** New ABC method that lets engines rewrite the message list every turn, BEFORE `pre_llm_call` and AFTER preflight compression.
- **Size:** ~30 LOC (1 ABC method default-noop, 1 call site in `run_agent.py` around line 12018, 1 test).
- **Why:** Enables always-on substitution cleanly. Eliminates the session-rotation hazard of forcing `should_compress=True` every turn.
- **Without it:** Use the experimental "force compress" fallback (ADR-010). Session ID rotates per turn, memory provider lineage breaks, log spam, compression-count warning trips in 2-3 turns. Not shippable for production.
- **Diff sketch:**
  ```python
  # agent/context_engine.py — add to ContextEngine ABC
  def preassemble(self, messages: List[Dict[str, Any]], budget_tokens: int = None) -> List[Dict[str, Any]]:
      """Optional per-turn assembly hook. Called BEFORE pre_llm_call,
      AFTER preflight compression. Engines that maintain an always-on
      substitution invariant (e.g. LCM) override this to rewrite the
      message list every turn. Default returns messages unchanged."""
      return messages
  ```
  ```python
  # run_agent.py around line 12018
  if hasattr(self.context_compressor, "preassemble"):
      messages = self.context_compressor.preassemble(messages, budget_tokens=...)
  ```
- **Evidence:** `docs/spike-results/002-hermes-pre-llm-call.md:139–166` (Option B section, "Cleanest path. Strongly preferred"). See ADR-010.

### Patch #2: `_EngineCollector.register_command` forwarding

- **What:** Make `_EngineCollector` in `plugins/context_engine/__init__.py` forward `register_command` calls to the wrapped `PluginContext` instead of silently dropping them. Same fix may apply to other forwarding methods.
- **Size:** ~15 LOC (~5 lines in `_EngineCollector.register_command` body + a test).
- **Why:** Today, context-engine plugins loaded via the **directory-mode loader** (`plugins/context_engine/<name>/`) can't register slash commands — `_EngineCollector.register_command` is documented as a no-op. This silently strips the `/lcm` command from `register()`.
- **Without it:** lossless-hermes can't ship as a directory-mode plugin AND register `/lcm`. Workaround is to ship as an entry-point plugin via `pyproject.toml` (which is the recommended path anyway — see plugin-glue.md), OR to register the command outside `register()` (awkward).
- **Evidence:** `docs/spike-results/002-hermes-pre-llm-call.md:21` ("`_EngineCollector.register_hook` is an explicit **no-op** — directory-mode context-engine plugins **cannot** register `pre_llm_call` hooks at all"). Same applies to `register_command`. This is a bug from LCM's perspective.

### Patch #3: `engine.ingest(message)` ABC method + call sites

- **What:** Add `ContextEngine.ingest(message: Dict)` ABC method (default no-op). Add ~25 call sites in `run_agent.py` at every place the messages list grows (user-message append, assistant-message append, tool-result append, etc.).
- **Size:** ~50 LOC (1 ABC method + 25 call site additions + 1 integration test).
- **Why:** Replaces the diff-on-each-turn workaround in ADR-009. Direct per-message ingest is cleaner, lower-latency, and cannot miss interrupted turns.
- **Without it:** Use ADR-009 workaround (`post_llm_call` diff + `handle_tool_call` diff). Works for 99% of turns; interrupted-then-resumed turns may have a brief window where partial state is unreachable.
- **Diff sketch:**
  ```python
  # agent/context_engine.py — add to ContextEngine ABC
  def ingest(self, message: Dict[str, Any]) -> None:
      """Per-message ingest hook. Called every time a new message
      lands in the conversation. Default no-op."""
      pass
  ```
  Then 25 call sites of `self.context_compressor.ingest(new_message)` in `run_agent.py`.
- **Evidence:** `docs/porting-guides/engine.md:95–101` (Option A in the ingest mechanism options). See ADR-009.

### Patch #4: Forward `cache_read_tokens` / `cache_write_tokens` in `update_from_response(usage)`

- **What:** When Anthropic returns `usage` with `cache_read_input_tokens` and `cache_creation_input_tokens` fields, forward them to `ContextEngine.update_from_response(usage)` so engines can implement cache-aware compaction deferral.
- **Size:** ~5 LOC (passthrough in the `usage` dict before calling `update_from_response`).
- **Why:** LCM's cache-aware deferral gate (`cache-aware-deferral-gate.test.ts`) makes decisions based on whether the last turn was a cache HIT. Without these counters being forwarded, the gate is blind.
- **Without it:** Cache-aware deferral degrades gracefully — without cache signals, LCM defaults to a conservative compaction policy. Functional, not optimal.
- **Diff sketch:**
  ```python
  # agent/llm_client.py around the usage normalization
  usage = {
      "prompt_tokens": resp.usage.input_tokens,
      "completion_tokens": resp.usage.output_tokens,
      "cache_read_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
      "cache_write_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0),
  }
  ```
- **Evidence:** Engine.md "cache-aware compaction" referenced throughout `cacheAwareCompaction` config and `compaction-telemetry-store` schema.

## Rationale

All four patches are:

- **Additive** — they extend the ABC and call surface without altering existing semantics. Default behavior unchanged for engines that don't override.
- **Non-breaking** — existing context engines (the built-in `compressor`) continue to function identically.
- **Small** — total ~100 LOC across the Hermes codebase.
- **Mutually beneficial** — any future ContextEngine plugin gets the same cleanup. LCM is the canonical first beneficiary.

The "ship workarounds, also propose patches" strategy lets v1 of lossless-hermes ship on the timeline that port-internal work determines, while opportunistically picking up upstream improvements as they merge. Worst case: all patches are rejected, and lossless-hermes ships with documented degradations.

Engine.md's open architecture decisions section (lines 514–527) flags each of these as a candidate. Spike 002 explicitly recommends patch #1 (line 184). Plugin-glue.md flags patch #2 (the `_EngineCollector` no-op is a real bug).

## Consequences

- **lossless-hermes ships without these patches in v1.** Workarounds documented in ADRs 009, 010, 014.
- **As patches merge, lossless-hermes adopts them in subsequent releases.** The plugin can detect via `hasattr(context_engine, "preassemble")` etc. and switch paths cleanly.
- **Open 4 upstream PRs (or 3 if patch #2 is folded into another).** Each PR small enough to review in one sitting. Pre-socialize with maintainer before filing.
- **Document the degradation modes.** v1 release notes should explain: "If Hermes >= X.Y.Z, behaves optimally. Otherwise, runs in experimental mode (always-on assembly via force-compress) and may show session-rotation warnings."
- **Maintainer relationship.** Filing 4 PRs to extend a public ABC is a significant ask. Mitigation: bundle them with a design note explaining how each patch benefits the ContextEngine ABC generally (not just LCM).
- **Risk that ALL patches are rejected.** lossless-hermes still ships with workarounds. No patches block release.

## Open questions / 5% uncertainty

- **Will the Hermes maintainer accept any of these?** Cannot know without filing. Best risk-mitigation: file patch #1 first (highest value); if accepted, file the rest.
- **Should patches be filed as one consolidated PR or four separate PRs?** Recommend separate — each can be reviewed in isolation, and a rejection on one doesn't block the others.
- **Patch #2 (`_EngineCollector.register_command`) may already be a known issue upstream.** Check `hermes-agent` issue tracker before filing — may already have a draft PR.
- **Hermes maintainer may prefer different API shapes.** E.g. `preassemble(messages, budget)` vs `transform_messages(messages, **context)`. Decide during PR review; LCM-side switch is trivial.
- **Patch #4 (cache tokens) depends on Anthropic's response shape.** If Hermes already normalizes usage dicts, this may be a 1-line change rather than 5. Investigate before filing.
- **Coordination with other downstream consumers.** The patches benefit any ContextEngine plugin, but no other such plugin exists yet (the built-in `compressor` doesn't need them). The maintainer review may push back on "speculative API surface for plugins that don't exist."
