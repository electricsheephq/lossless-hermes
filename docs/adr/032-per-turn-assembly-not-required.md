# ADR-032: Per-turn assembly is not required; adopt ingest + threshold/debt-gated compaction

**Status:** Accepted
**Date:** 2026-05-19
**Confidence:** 95%
**Supersedes:** ADR-010
**Superseded by:** —

> **Implementation is v0.2.0-tracked.** This ADR records the *decision* reached by
> the `hermes-lcm` architecture review at ≥95% confidence. The code changes it
> describes (demoting `preassemble`, adding the debt-gated compaction path, and the
> dead-code removal in `engine/assemble.py`) are scheduled for v0.2.0 — see
> [GitHub issue #132](https://github.com/electricsheephq/lossless-hermes/issues/132).
> v0.1.0 ships unchanged on the compress-every-turn path.

## Context

ADR-010 made the v0.1.0 port's critical path depend on an *always-on assembly
substitution* mechanism: the belief that LCM, to reach losslessness, **must** rewrite
the entire message list from the DAG (`contextItems` + summaries) under a token
budget **on every turn, before the LLM sees the messages**. ADR-010 concluded that
because Hermes has no per-turn message-replacement hook (`pre_llm_call` is additive
— spike 002, lines 49–51), the only shippable path was an upstream Hermes ABC patch
adding `ContextEngine.preassemble(messages, budget) → messages` — drafted as
upstream PR #1 in ADR-015 and **filed as
[NousResearch/hermes-agent#24949](https://github.com/NousResearch/hermes-agent/pull/24949)**.
Until that PR merged, ADR-010 said the port could only ship "experimental" via
forcing `should_compress() → True` every turn (the Option-A "force-compress" path).

That whole framing rested on an unverified premise: that **per-turn full-message-list
rewrite is intrinsic to LCM**.

The `hermes-lcm` architecture review (slice S1, 95% confidence) tested that premise
against a second, independent production system. **`stephenschoettler/hermes-lcm`**
(~540★, shipping today — see `docs/related-work.md`) is a Hermes-Agent LCM plugin
that reaches losslessness **without any per-turn rewrite and without any session
rotation**. It does so with:

1. **Per-message ingest** — every new message is captured into the DAG store as it
   lands (the same mechanism this repo already chose in ADR-009 via `post_llm_call`
   diffing). This is what makes the store *lossless*: nothing is lost because
   everything is ingested.
2. **Threshold/debt-gated incremental compaction** — compaction fires only when the
   live prompt crosses a token threshold (or when accumulated *raw-backlog debt*
   crosses a maintenance threshold), not every turn. When it fires, it folds the
   oldest raw turns into summaries incrementally. Hermes core's own
   `ContextEngine.compress()` seam (introduced by Hermes PR #7464, merged) is the
   replacement point — fired by the preflight check at `run_agent.py` only when the
   threshold gate trips.

The distinction the review draws is the crux of this ADR:

> **Per-turn full-message-list rewrite is a property of the *pyramid assembly
> algorithm* — one particular way to realize LCM — not a property of *LCM in
> general*.** Losslessness comes from *ingest* (capture everything). Context-fit
> comes from *compaction* (fold the oldest raw turns when over budget). Neither
> requires rewriting the message list on a turn where nothing crossed a threshold.

ADR-010's per-turn-rewrite requirement conflated the two. The pyramid algorithm
*chooses* to re-derive the assembled view every turn because that is convenient given
its data structures; it is a latency/simplicity choice inside the algorithm, not an
external correctness invariant LCM-on-Hermes must satisfy.

This reframing forces a choice for v0.2.0: keep PR #24949 (`preassemble`) on the
critical path as ADR-010 dictates, or demote it and adopt the ingest +
threshold/debt-gated compaction path that `hermes-lcm` proves is sufficient.

### Correcting `docs/upstream/001a`

`docs/upstream/001a-preassemble-vs-7464-investigation.md` reached the *opposite*
wrong conclusion. Faced with the same observation — that Hermes PR #7464 already
ships a `compress()` substitution seam — 001a concluded that the **"Option A
force-compress" path is production-grade** and should be promoted to canonical (001a
"Why 'Option A' is actually production-grade", lines 87–98; "the compress-every-turn
approach is ... no flag needed").

**That claim is wrong**, and this ADR records why. Force-compress reaches the
`compress()` seam by routing through Hermes's `_compress_context`, and
`_compress_context` **rotates the SQLite session every time it runs**
(`run_agent.py:10311–10337`, catalogued in spike 002, lines 130–137). Forcing it
every turn means:

- a fresh `session_id` per turn — gateway routing, memory-provider lineage,
  langfuse traces, and `parent_session_id` chains all break;
- `commit_memory_session(messages)` re-extracts every turn;
- the "Session compressed N times — accuracy may degrade" warning trips in 2–3
  turns;
- the file-read dedup cache resets every turn.

Spike 002's verdict on exactly this path stands: **"NOT shippable — it breaks
session lifecycle, memory provider lineage, and observability"** (spike 002, line
137). 001a's error was assuming that *because `compress()` is the only ABC method
that returns a new list*, forcing it every turn is therefore the intended production
mechanism. It is not — `compress()` is intended to fire **on the threshold gate**,
which is precisely the threshold/debt-gated path this ADR adopts. The session
rotation is not a side effect to tolerate; it is the signal that you are using
`compress()` outside its contract.

So both prior documents are corrected here: ADR-010 was wrong that per-turn rewrite
is *required*; 001a was wrong that per-turn `compress()` is *production-grade*. The
correct path is neither — it is **ingest every message, compact only on a gate**.

## Options considered

### Option A: Keep ADR-010 — `preassemble` (PR #24949) stays on the critical path

- **Description:** Hold the v0.1.0/v1.0 line that always-on per-turn assembly is
  mandatory. Block a stable release on NousResearch/hermes-agent#24949 merging. Ship
  experimental force-compress until then.
- **Pros:**
  - No change to existing ADR-010 reasoning or to the already-merged
    `engine/assemble.py` `preassemble()` override (issue 03-09 / PR #56).
  - If the pyramid algorithm's per-turn re-derivation genuinely produced
    materially better recall, this path preserves it.
- **Cons:**
  - **Premise is now falsified.** A 540★ production system (`hermes-lcm`) reaches
    losslessness with no per-turn rewrite. "Required" is not true.
  - **Critical path depends on an external maintainer.** PR #24949 is unmerged;
    ADR-010 itself rated its own confidence 60% precisely because "outcome hinges on
    upstream maintainer acceptance." Keeping a release gate on a third-party PR is a
    standing schedule risk with no owner.
  - **Force-compress fallback is not shippable** (spike 002 line 137). A release
    "gated on experimental mode" is a release that cannot actually ship to
    production operators.
  - Carries dead code: the merged `preassemble()` override in `engine/assemble.py`
    is never invoked by Hermes core (the upstream ABC has no `preassemble` method —
    001a evidence, lines 24–61), so it is dead today and stays dead unless #24949
    merges.
- **Evidence cited:** ADR-010 §Decision, §Open questions (self-rated 60%);
  spike 002 lines 125–137; `docs/upstream/001a` lines 24–61.

### Option B: Drop the per-turn-rewrite requirement; adopt ingest + threshold/debt-gated compaction

- **Description:** Treat per-turn full-message-list rewrite as a property of the
  pyramid *algorithm*, not of LCM-on-Hermes. Reach losslessness through:
  1. **Per-message ingest** (already ADR-009: `post_llm_call` diffing +
     `handle_tool_call` safety net) — every message enters the DAG store.
  2. **Threshold/debt-gated incremental compaction** — when the live prompt crosses
     a token threshold *or* accumulated raw-backlog debt crosses a deferred-
     maintenance threshold, fold the oldest raw turns into summaries. Use Hermes's
     `compress()` seam on the threshold gate (its intended contract — no per-turn
     forcing, so **no session rotation**).
  Demote PR #24949 (`preassemble`) from the critical path to an **optional latency
  optimization**: if it ever merges upstream, an engine *may* override `preassemble`
  to pre-shape the message list and shave the first-token latency of a
  threshold-crossing turn — but the port does not depend on it and ships fully
  without it.
- **Pros:**
  - **Matches a proven production system.** `hermes-lcm` (540★) is the existence
    proof that this is sufficient for losslessness.
  - **No release gated on a third-party PR.** The critical path uses only
    already-merged Hermes core (`ContextEngine` + `compress()`, Hermes PR #7464).
  - **No session rotation.** `compress()` fires on its intended threshold gate, so
    `_compress_context`'s session rotation happens only on a real compaction —
    exactly as Hermes core's own compressor behaves. Gateway routing, memory
    lineage, langfuse, and `parent_session_id` chains stay intact.
  - **A raw-backlog-debt path keeps losslessness honest under deferred
    maintenance.** Even when the prompt never crosses the token threshold (short
    sessions), accumulated un-summarized raw turns are tracked as *debt*; once debt
    crosses its own threshold a maintenance compaction runs. This prevents an
    unbounded raw backlog and keeps the DAG's summary tier current without per-turn
    work.
  - **Removes dead code.** The unused `preassemble()` override in
    `engine/assemble.py` and the misleading `experimental_always_on_via_compress`
    flag / `_emit_experimental_warning_if_due` warning can be deleted (v0.2.0).
- **Cons:**
  - **Demotes work already filed.** PR #24949 was drafted and filed; demoting it
    means that effort becomes an optional nice-to-have rather than a v1.0
    dependency. Mitigation: it is genuinely still useful as a latency optimization;
    it is not discarded, only re-scoped.
  - **The pyramid algorithm's per-turn re-derivation is not replicated 1:1.** If a
    future benchmark showed per-turn re-derivation produced materially better
    recall than threshold-gated compaction, this path would need revisiting.
    Mitigation: `hermes-lcm` is lossless without it — the burden of proof is on
    showing per-turn rewrite *adds* recall, and no such measurement exists.
  - **Compaction-quality now depends on the threshold + debt tuning.** Thresholds
    set too high let the raw backlog grow; too low compact too eagerly. Mitigation:
    the threshold gate is exactly Hermes core's existing `should_compress` knob;
    the debt threshold is a new but bounded operator knob.
- **Evidence cited:** `hermes-lcm` architecture review slice S1 (95%);
  `docs/related-work.md` (`hermes-lcm` is a shipping Hermes-LCM plugin against the
  same PR #7464 slot); ADR-009 (per-message ingest already chosen); Hermes PR #7464
  (`ContextEngine` + `compress()` seam, merged — `docs/related-work.md` line 93).

### Option C: Keep `preassemble` on the critical path but also build the debt-gated path as a fallback

- **Description:** Build both. Ship the threshold/debt-gated path, but also keep
  `preassemble` as the "preferred" mechanism once #24949 merges, switching the
  engine over at that point.
- **Pros:** Hedges — if `preassemble` merges, the port can adopt it.
- **Cons:**
  - **Two assembly code paths** for the same outcome doubles the test matrix and
    the reviewer surface, for a benefit (per-turn re-derivation) that has no
    measured value over the debt-gated path.
  - **Keeps the `experimental_*` flag and dead-code ambiguity alive** — the
    documentation-embarrassment 001a flagged ("the warning log says experimental
    but it's not") persists.
  - The "switch the engine over when #24949 merges" step is a future migration with
    no triggering owner — it tends never to happen, leaving two paths forever.
- **Evidence cited:** same as Option B; the two-path cost mirrors the Option-C
  rejection rationale in ADR-031 ("a second code path for the same behaviour").

## Decision

Chosen: **Option B — drop the per-turn-rewrite requirement. Adopt per-message
ingest + threshold/debt-gated incremental compaction as the critical path. Demote
NousResearch/hermes-agent#24949 (`preassemble`) to an optional latency
optimization.**

Per-turn full-message-list rewrite is hereby recorded as a property of the **pyramid
assembly algorithm**, not of LCM-on-Hermes. The losslessness invariant is satisfied
by ingest (ADR-009); the context-fit invariant is satisfied by threshold/debt-gated
compaction through Hermes's already-merged `compress()` seam. The release no longer
depends on any unmerged upstream Hermes PR.

This is the path `stephenschoettler/hermes-lcm` ships in production at 540★, and the
`hermes-lcm` architecture review reached 95% confidence that it is sufficient for
losslessness on Hermes.

## Rationale

1. **The "required" premise is empirically falsified.** ADR-010 asserted always-on
   per-turn assembly is LCM's "load-bearing architectural property" and built the
   whole critical path around it. The architecture review's slice S1 found a 540★
   production Hermes-LCM plugin that is lossless **without** it. An architectural
   requirement that a shipping production system demonstrably does not need is not a
   requirement — it is one algorithm's implementation detail. Confidence 95%: the
   only residual doubt is whether per-turn rewrite buys *recall* (not losslessness),
   and no measurement supports that.

2. **A release must not be gated on a third party's merge queue.** ADR-010 self-
   rated 60% confidence because "outcome hinges on upstream maintainer acceptance"
   of PR #24949. That is an unowned, unbounded schedule risk. Option B's critical
   path uses only already-merged Hermes core (PR #7464's `ContextEngine` +
   `compress()`). The release becomes self-contained.

3. **The force-compress fallback was never shippable, so ADR-010 had no real v1.0
   path.** Spike 002 (line 137) is unambiguous: forcing `compress()` every turn
   "breaks session lifecycle, memory provider lineage, and observability." ADR-010's
   "ship experimental until #24949 merges" was therefore not a path to a production
   release at all — it was a path to a perpetually-experimental build. Option B
   gives the port an actual production path on day one.

4. **`docs/upstream/001a` reached the inverse error and must be corrected.** 001a
   saw the `compress()` seam and concluded force-compress is "production-grade"
   (001a lines 87–98). It is not: `compress()` rotates the session
   (`run_agent.py:10311–10337`) and is contracted to fire on the threshold gate, not
   every turn. The correct reading of PR #7464 is the one this ADR takes —
   `compress()` *is* the compaction seam, used **on the gate**. 001a's "no flag
   needed, compress-every-turn is canonical" guidance is superseded by this ADR and
   must not be actioned.

5. **The raw-backlog-debt path keeps losslessness honest without per-turn work.**
   Per-message ingest guarantees nothing is *lost*; but a long-lived short session
   could accumulate a large un-summarized raw backlog and never cross the token
   threshold, leaving the summary tier stale. Tracking accumulated un-summarized raw
   turns as *debt* and running a maintenance compaction when debt crosses its own
   threshold closes that gap. This is deferred maintenance — it runs when there is
   debt to pay down, not on a fixed per-turn cadence — so it preserves the "no
   per-turn rewrite" property while keeping the DAG current.

6. **`preassemble` is not discarded — it is correctly re-scoped.** If
   NousResearch/hermes-agent#24949 merges, an engine *may* override `preassemble` to
   pre-shape the message list immediately before a threshold-crossing turn and shave
   first-token latency. That is a real, if modest, optimization. Demoting it to
   "optional latency optimization, not a correctness dependency" is the accurate
   scoping — and it removes the dead-code/experimental-flag ambiguity from the
   critical path.

## Consequences

- **ADR-010 is superseded.** Its status line is updated to
  `Superseded by ADR-032`. Its text is preserved unchanged (ADRs are append-only per
  CLAUDE.md "Don't change an ADR's decision without writing a new ADR that
  supersedes it").

- **v0.2.0 work (issue #132):**
  - `engine/` gains a **threshold/debt-gated compaction path**: compaction fires
    when the live prompt crosses the token threshold *or* when accumulated
    raw-backlog debt crosses a deferred-maintenance threshold. The token-threshold
    arm routes through Hermes's `compress()` seam (its intended contract). The debt
    arm runs a maintenance compaction over the oldest un-summarized raw turns.
  - **Remove dead code** in `engine/assemble.py`: delete the `preassemble()`
    override (never invoked — the upstream ABC has no `preassemble` method),
    delete or invert the `experimental_always_on_via_compress` config flag, and
    delete the misleading `_emit_experimental_warning_if_due` per-turn warning.
    Update the `engine/assemble.py` module docstring (its current header — quoted in
    001a lines 117–122 — describes both an "Option A" and "Option B" that this ADR
    retires).
  - Update the issue 03-09 spec
    (`epics/03-ingest-assembly/03-09-always-on-substitution-hook.md`) to reflect
    that per-turn substitution is no longer the mechanism.
  - A new operator knob for the **raw-backlog-debt threshold** is added (bounded;
    documented alongside the existing `should_compress` token threshold).

- **`preassemble` (PR #24949) is demoted, not closed.** ADR-015's upstream-patch
  tracking is updated: patch #1 (`preassemble`) moves from "v1.0 dependency" to
  "optional latency optimization — not on the critical path." `docs/upstream/001-preassemble-abc.md`
  is annotated to that effect. The PR may stay open upstream as a latency
  enhancement; if it merges, an engine *may* (not must) override `preassemble`.

- **`docs/upstream/001a` is corrected.** A correction note is added at the top of
  `001a-preassemble-vs-7464-investigation.md` pointing to this ADR and stating that
  001a's central claim — that the Option-A force-compress / compress-every-turn path
  is "production-grade" and should be canonical — is **wrong** (force-compress
  rotates the session every turn; `compress()` is contracted to fire on the
  threshold gate). 001a's other observations (PR #7464 exists; the upstream ABC has
  no `preassemble`; the merged `preassemble()` override is dead code) remain
  accurate and are used by this ADR.

- **ADR-009 (per-message ingest) is unaffected and load-bearing.** It is the
  losslessness half of this decision. Its `post_llm_call` diffing +
  `handle_tool_call` safety net stay exactly as specified.

- **No session rotation on a non-compacting turn.** Because `compress()` is invoked
  only on the threshold gate, `_compress_context`'s session rotation
  (`run_agent.py:10311–10337`) fires only on a genuine compaction — identical to
  Hermes core's built-in compressor. This is the property force-compress destroyed
  and that this decision restores.

- **Invariant — losslessness comes from ingest.** Every message MUST be ingested
  into the DAG store within the session lifetime (ADR-009's coverage guarantee). No
  compaction strategy may drop a raw message before it has been ingested and
  (eventually) summarized. Compaction folds raw turns into summaries; it never
  discards un-ingested content.

- **Invariant — compaction is gate-driven, never per-turn.** No code path may force
  `compress()` (or any successor compaction routine) to run unconditionally every
  turn. Compaction fires on the token threshold or the raw-backlog-debt threshold —
  never on a fixed per-turn cadence. This is what keeps session rotation, memory
  lineage, and observability intact.

- **v0.1.0 ships unchanged.** v0.1.0 is already released (`988e425`, v0.1.1). This
  ADR changes nothing already shipped; it scopes v0.2.0 engine work. v0.1.0's
  current compress-every-turn behavior is a known v0.1.0 limitation, addressed by
  the #132 work.

## Open questions / 5% uncertainty

1. **Does per-turn re-derivation buy measurable recall over threshold-gated
   compaction?** The review's 95% confidence covers *losslessness* — `hermes-lcm`
   proves the gated path is lossless. It does not prove the two paths have *equal
   recall quality*. The pyramid algorithm's per-turn re-derivation could, in
   principle, surface recovered context-items more aggressively. Mitigation: this is
   an Epic-09-style eval question — run a recall benchmark of the debt-gated path
   against the (experimental) per-turn path on a shared corpus before v0.2.0 closes.
   If the gated path loses materially, a follow-up ADR revisits. No evidence
   currently suggests it will.

2. **Where exactly is the raw-backlog-debt threshold set, and in what unit?**
   Candidates: count of un-summarized raw turns, or token sum of the un-summarized
   raw backlog. Token sum is more directly tied to context pressure; turn count is
   simpler to reason about. Decide during v0.2.0 design; default leaning is token
   sum, to compose cleanly with the existing token-threshold gate.

3. **Interaction with Hermes's own preflight compaction.** Hermes core's preflight
   already calls `compress()` on its threshold. The LCM engine *is* the registered
   `context_compressor`, so the LCM compaction and Hermes's preflight are the same
   call. Confirm during v0.2.0 that the debt-gated maintenance compaction (which
   fires *outside* a token-threshold crossing) has a clean invocation point — likely
   `on_session_start` housekeeping or a post-turn hook — and does not double-fire
   with the preflight path.

4. **Should the demoted `preassemble` override be deleted now or kept as a stub?**
   This ADR's consequence section says delete it in v0.2.0 (it is dead code). The
   alternative — keep it as an inert, documented stub in case #24949 merges — was
   considered and rejected: a dead stub is exactly the ambiguity 001a complained
   about. If #24949 merges later, re-adding a `preassemble` override is a small,
   well-scoped change at that time.

5. **Upstream may still merge #24949.** If NousResearch merges it, nothing breaks —
   the port simply gains an *optional* latency hook it may adopt. This ADR's
   decision (gated compaction is the critical path) does not change. A future ADR
   would only be needed if the team decided to make `preassemble` the *primary*
   assembly mechanism — which would require the recall evidence open question (1)
   above to come back in its favor.
