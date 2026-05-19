# ADR-036: Entity + synthesis subsystem — keep for port fidelity, with an Epic-09 eval kill-criterion

**Status:** Accepted
**Date:** 2026-05-19
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —
**Implementation:** v0.2.0-tracked (doc-only ADR; the kill-criterion *evaluation* is Epic-09 work)
**Issue:** [electricsheephq/lossless-hermes#136](https://github.com/electricsheephq/lossless-hermes/issues/136)

## Context

`lossless-hermes` ships two substantial subsystems — **entity coreference
extraction** (`src/lossless_hermes/extraction/`: the async, queue-driven
coreference worker that upserts `lcm_entities` / `lcm_mentions`) and
**synthesis dispatch** (`src/lossless_hermes/synthesis/`: the on-demand
tier-dispatched orchestrator behind `lcm_synthesize_around`, with the
`lcm_prompt_registry` / `lcm_synthesis_cache` / `lcm_synthesis_audit` tables).
Together they are roughly **5,500 LOC**, the whole of Epic 07, and they back
three of the eight `lcm_*` tools (`lcm_get_entity`, `lcm_search_entities`,
`lcm_synthesize_around`).

These subsystems are **not** speculative scope. They are a faithful port of
**real, wired `lossless-claw` v4.1 functionality**: the coreference worker and
the synthesis dispatcher both exist in the `pr-613` TypeScript source at
commit `1f07fbd`, both are exercised by upstream tests, and both carry 12
waves of audit-fix scar tissue (Wave-1 race-safe `INSERT OR IGNORE`, Wave-4
P0 prompt-injection defense, Wave-7 per-row `SAVEPOINT` discipline, Wave-10
filter parity — see `epics/07-entity-synthesis/README.md`). Per **ADR-030**,
v0.1.0's mandate is a **verbatim 1:1 port of `pr-613`** — "covering ... full
retrieval + synthesis + embeddings + extraction" (ADR-030 §Decision). Under
that mandate, porting entity + synthesis is not a choice; it is the contract.

But the architecture review against the sibling project surfaced a tension
that the verbatim-port mandate had quietly buried. Slice **S7** (95%
confidence) observed:

> A 540★ production sibling (`hermes-lcm`) ships with **neither** an entity
> subsystem **nor** a synthesis subsystem — and works.

So two things are simultaneously true:

- Entity + synthesis are **load-bearing for the port's verbatim-`pr-613`
  contract** (ADR-030). Dropping them would make v0.1.0 *not* a faithful port.
- Entity + synthesis are **deferrable for a *minimal* LCM product** — a
  production sibling demonstrates a shipping LCM plugin without either.

The combination is an **unexamined assumption**: v0.1.0 carries ~5,500 LOC of
subsystem on the strength of "the port mandate says so," with no recorded
check on whether that subsystem actually earns its keep on **Hermes** traffic
(as opposed to OpenClaw traffic, where it was tuned). The constraint forcing a
choice: do we leave that assumption unexamined, or convert it into a tracked,
**falsifiable** decision with an exit ramp?

This is a **doc-only ADR**. No code changes in v0.1.x. Its job is to record
the keep-decision *and* the criterion that could later reverse it.

## Options considered

### Option A: Keep entity + synthesis, with a falsifiable Epic-09 eval kill-criterion

- **Description:** v0.1.x **keeps both subsystems**, unchanged — the
  verbatim-`pr-613` mandate (ADR-030) stands and v0.1.0 ships a faithful port.
  But this ADR records a **kill-criterion**: Epic-09's recall eval measures
  the *retrieval lift* attributable to entity-coreference recall on real
  Hermes traffic. **If that contribution falls below a defined threshold**
  (proposed: **< 5 percentage points** of measured retrieval lift — see
  Rationale for why 5pp), the decision flips: both subsystems are **demoted to
  an optional plugin-extra** (an opt-in install group, off by default) in
  **v0.3**. The criterion is falsifiable — there is a concrete measurement,
  from a named epic, that can prove the keep-decision wrong.
- **Pros:**
  - **Preserves the v0.1.0 port-fidelity contract.** ADR-030's verbatim
    mandate is honoured; v0.1.0 is, and stays, a faithful 1:1 port of
    `pr-613`. No scope is cut on a hunch.
  - **Converts the unexamined assumption into a tracked decision.** The review
    found a buried assumption; this option surfaces it, names it, and attaches
    a measurement that can resolve it. The next person sees a decision, not a
    silent inheritance.
  - **Falsifiable, with a real exit ramp.** Epic-09 already builds the recall
    eval surface (`src/lossless_hermes/eval/recall.py` — recall@K,
    reciprocal-rank, per-stratum aggregates). The kill-criterion plugs into a
    measurement that *exists*, so it is genuinely testable rather than
    aspirational.
  - **Demotion, not deletion, is reversible and cheap to user.** "Optional
    plugin-extra" means the code stays in-tree and installable
    (`pip install lossless-hermes[entities]`-style), just off the default
    path. Operators who need entity recall still get it; the default install
    gets lighter. If a later eval flips the call back, re-promoting is a
    default-flag change.
  - **Mirrors how the sibling's evidence is used honestly.** `hermes-lcm`
    shipping without these subsystems is *evidence that they may be
    deferrable* — not proof. Option A treats it as a hypothesis to test on our
    own traffic, which is the correct epistemic weight for a single
    data-point.
- **Cons:**
  - **The criterion is only as good as Epic-09's eval.** If the eval set does
    not include enough entity-recall-sensitive queries, the measured
    contribution is unreliable and the kill-criterion can fire (or fail to
    fire) for the wrong reason. Mitigation: the eval query set must include a
    stratum that specifically exercises cross-reference / entity-coreference
    recall before the criterion is evaluated (see Open questions).
  - **Carries ~5,500 LOC through v0.1.x on a *maybe*.** Until Epic-09 traffic
    data exists, v0.1.x ships the full subsystem. That is real maintenance
    surface for code that might be demoted. Counter: it is *ported, tested,
    audit-fixed* code, not new risk — and ADR-030's mandate requires it in
    v0.1.0 regardless.
  - **A threshold chosen now is a judgement call.** 5pp is proposed, not
    measured. If it is wrong, the criterion mis-fires. Mitigation: the
    threshold is explicitly a *proposal* this ADR records; Epic-09 may refine
    it with a follow-up ADR once the eval's noise floor is known.
- **Evidence cited:**
  - Architecture review slice S7 (95%): `hermes-lcm` (540★) ships without
    either subsystem and works.
  - ADR-030 §Decision — v0.1.0 is a verbatim 1:1 port of `pr-613` covering
    "full ... synthesis ... extraction."
  - `epics/07-entity-synthesis/README.md` — the subsystems are real, wired
    `pr-613` functionality with 12 waves of audit fixes.
  - `epics/09-eval/README.md` — Epic-09 builds the recall eval surface
    (`eval/recall.py`: recall@K, per-stratum) the kill-criterion measures
    against.

### Option B: Keep entity + synthesis with no kill-criterion (status quo)

- **Description:** Carry both subsystems indefinitely because ADR-030's
  verbatim mandate says so. Record nothing further.
- **Pros:**
  - Zero new documentation. The port mandate is already on record.
  - No threshold to argue about.
- **Cons:**
  - **Leaves the assumption exactly as buried as the review found it.** The
    review's whole point in S7 is that "the port mandate says so" is not the
    same as "this subsystem earns its keep on Hermes traffic." Option B does
    not address the finding.
  - **No exit ramp.** If entity recall turns out to contribute nothing on
    Hermes traffic, there is no recorded trigger and no agreed action — the
    5,500 LOC is carried forever by default.
  - **Wastes the sibling's evidence.** A 540★ production counter-example is a
    genuine signal; status quo files it under "interesting" and acts on
    nothing.

### Option C: Drop entity + synthesis from v0.1.0 now (match `hermes-lcm`)

- **Description:** Cut both subsystems immediately; ship v0.1.0 as a minimal
  LCM matching `hermes-lcm`'s surface.
- **Pros:**
  - ~5,500 LOC lighter; smaller v0.1.0 surface and review burden.
  - Matches the production sibling's shape directly.
- **Cons:**
  - **Breaks the ADR-030 verbatim-port contract.** v0.1.0 would no longer be a
    faithful 1:1 port of `pr-613` — and per CLAUDE.md, changing a prior ADR's
    decision requires a *superseding* ADR, not a side decision. Dropping
    synthesis/extraction is exactly an ADR-030 reversal.
  - **Acts on one data-point as if it were proof.** `hermes-lcm` shipping
    without these subsystems shows it is *possible*, not that entity recall is
    valueless on Hermes traffic. Cutting now is a speculative scope cut — the
    precise move the review methodology (and ADR-031's "defer until eval
    data") warns against.
  - **Discards 12 waves of audit-fix scar tissue.** The coreference worker and
    synthesis dispatcher carry hard-won race / injection / parity fixes
    (Wave-1/4/7/10). Dropping the subsystem throws that away; re-adding later
    means re-earning it.
  - **Loses byte-compat with OpenClaw `lcm.db`.** ADR-025's migration story
    assumes the `lcm_entities` / `lcm_synthesis_*` tables exist. Cutting the
    subsystems complicates — or breaks — the import path for existing OpenClaw
    users.

## Decision

Chosen: **Option A — v0.1.x keeps the entity-coreference and synthesis
subsystems (the ADR-030 verbatim-`pr-613` mandate stands), AND this ADR records
a falsifiable kill-criterion: if Epic-09's recall eval shows that
entity-coreference recall contributes less than 5 percentage points of
measured retrieval lift on real Hermes traffic, both subsystems are demoted to
an optional, off-by-default plugin-extra in v0.3.**

This is a **doc-only ADR**. It changes no code in v0.1.x. It converts an
unexamined assumption ("we carry 5,500 LOC because the port mandate says so")
into a tracked, falsifiable decision with a concrete measurement and a defined
action.

## Rationale

1. **Port fidelity is a real, recorded contract — honour it.** ADR-030's
   decision is that v0.1.0 is a verbatim 1:1 port of `pr-613`, explicitly
   including synthesis and extraction. Entity + synthesis are wired,
   audit-fixed `pr-613` functionality, not speculative scope. Cutting them
   (Option C) reverses ADR-030 and, per CLAUDE.md, would itself require a
   superseding ADR backed by evidence we do not yet have. Option A keeps the
   contract intact.

2. **But "the mandate says so" is not evidence the subsystem earns its
   keep.** Slice S7's value is precisely that it separates two questions the
   verbatim mandate had merged: *is this a faithful port?* (yes — keep it) and
   *does this subsystem pull its weight on Hermes traffic?* (unknown — measure
   it). A 540★ production sibling shipping without either subsystem is a
   credible signal that the second answer might be "no." Option A is the only
   choice that records both answers honestly.

3. **The kill-criterion must be falsifiable, and Epic-09 makes it so.**
   Epic-09 already ships the recall eval surface — `eval/recall.py` computes
   recall@K, reciprocal-rank, and per-stratum aggregates, and the Voyage
   benchmark already measures retrieval lift in percentage points (the
   +52.5pp paraphrastic figure). A kill-criterion expressed as "entity-recall
   contribution < 5pp of measured retrieval lift" plugs directly into a
   measurement that *exists*. It is genuinely testable, not aspirational.

4. **Why 5 percentage points (proposed).** The Voyage hybrid-retrieval
   decision in v4.1 was justified by a **+52.5pp** paraphrastic recall lift —
   that is the scale at which a retrieval subsystem is considered clearly
   worth its weight in this project. A 5pp floor is roughly one-tenth of that
   bar: it says entity-coreference recall must contribute at least a *modest,
   measurable* slice of retrieval lift to justify ~5,500 LOC and three tools
   on the default path. Below 5pp, the subsystem is within plausible eval
   noise of contributing nothing, and the cost no longer obviously pays for
   itself. The figure is a **proposal** this ADR records — Epic-09 may refine
   it with a follow-up ADR once the eval's measured noise floor (per
   `epics/09-eval/README.md` §residual-risk: "asserted at ±1e-9" for aggregate
   stability, plus the eva-baseline-v2 provenance caveat) is known.

5. **Demotion ≠ deletion — the exit ramp is cheap and reversible.** If the
   criterion fires, the v0.3 action is to move both subsystems behind an
   optional install extra (off by default), not to delete them. The code stays
   in-tree, the audit-fix scar tissue is preserved, and operators who need
   entity recall opt in. If a later, richer eval flips the call back,
   re-promotion is a default-flag change. This makes acting on the criterion
   low-regret in both directions.

6. **Treating one data-point as a hypothesis is the correct epistemics.**
   `hermes-lcm`'s shape is evidence, not proof. Option C spends that evidence
   as if it settled the question; Option B ignores it. Option A spends it
   correctly — as a hypothesis worth a real test on our own traffic — which is
   the same disciplined "defer the opinionated choice until eval data exists"
   stance ADR-031 took for the synthesis tier ladder.

Option B was rejected because it leaves the review's finding unaddressed and
provides no exit ramp. Option C was rejected because it reverses ADR-030
without a superseding evidence base and acts on a single data-point as if it
were proof.

## Consequences

- **No v0.1.x code change.** This ADR is doc-only. Entity (`extraction/`) and
  synthesis (`synthesis/`) ship in v0.1.0 exactly as the verbatim-`pr-613`
  port (ADR-030) requires, backing `lcm_get_entity`, `lcm_search_entities`,
  and `lcm_synthesize_around`.
- **A kill-criterion is now on record.** The criterion: *entity-coreference
  recall contributes < 5pp of measured retrieval lift on real Hermes traffic,
  as measured by Epic-09's recall eval.* The action if it fires: *demote both
  subsystems to an optional, off-by-default plugin-extra in v0.3.*
- **Epic-09 owns the measurement.** Evaluating the kill-criterion is an
  Epic-09 / v0.2.0-tracked task: Epic-09's recall eval (`eval/recall.py`,
  per-stratum recall@K) must produce an entity-recall-attributable lift
  number. The eval query set must include a stratum that exercises
  cross-reference / entity-coreference recall, or the measurement is not
  trustworthy (see Open questions).
- **If the criterion fires, v0.3 demotes — a follow-up ADR records the flip.**
  The v0.3 change is: move `extraction/` + `synthesis/` behind an optional
  install group (e.g. `lossless-hermes[entities]`), default off; the three
  entity/synthesis tools register only when the extra is installed. That flip
  is a new ADR (037+) citing the Epic-09 measurement — consistent with
  CLAUDE.md's append-only ADR rule.
- **If the criterion does NOT fire, the keep-decision is confirmed** and this
  ADR stands as the record that the assumption was examined and held.
- **The OpenClaw migration story is unaffected in v0.1.x.** ADR-025's import
  path keeps working because the `lcm_entities` / `lcm_synthesis_*` tables
  remain present. Any v0.3 demotion must keep the migration ladder creating
  those tables (the *data* is still importable; only the *active subsystem* is
  opt-in) — a constraint for the future ADR, noted here.
- **Invariant:** v0.1.x does not act on the kill-criterion. The criterion is a
  v0.3 trigger; v0.1.x ships the full subsystem regardless of any partial or
  early eval signal.
- **Invariant:** the kill-criterion is evaluated on **real Hermes traffic**,
  not OpenClaw traffic and not a synthetic fixture alone. The subsystems were
  tuned on OpenClaw; the open question is their value on Hermes. An eval on
  the wrong traffic does not satisfy the criterion.
- **`epics/07-entity-synthesis/README.md` and `epics/09-eval/README.md`** gain
  a cross-reference to this ADR (carried by the v0.2.0 implementation/tracking
  issue, not this doc-only ADR).

## Open questions / 5% uncertainty

1. **Does Epic-09's eval set exercise entity recall enough to trust the
   number?** The kill-criterion measures entity-coreference recall
   contribution, but `epics/09-eval/README.md` notes the eva-baseline-v2
   fixture is 14 fts-easy / 9 fts-medium / 8 paraphrastic — strata chosen for
   FTS-vs-hybrid comparison, not specifically for entity coreference. Before
   the criterion is evaluated, the eval set must add (or be confirmed to
   contain) a stratum of queries whose correct retrieval *depends on*
   entity-coreference resolution. Otherwise a low measured contribution may
   just mean the eval never tested the thing. Flag for the Epic-09 follow-up.
2. **Is 5pp the right threshold?** It is proposed by analogy to the +52.5pp
   Voyage bar (≈ one-tenth of it). The true floor depends on Epic-09's
   measured noise — if per-stratum recall@K has a noise band wider than ~5pp
   at n≈31 queries, the threshold must rise above the noise floor or the
   eval set must grow. A follow-up ADR may refine the number once the noise
   floor is measured.
3. **Entity vs synthesis may not deserve the *same* verdict.** This ADR treats
   entity-coreference and synthesis as one keep-or-demote unit because S7
   raised them together and `hermes-lcm` drops both. But synthesis
   (`lcm_synthesize_around`) and entity recall (`lcm_get_entity` /
   `lcm_search_entities`) are separable — it is conceivable the eval shows
   entity recall is weak while on-demand synthesis is valued (or vice versa).
   If the Epic-09 data splits cleanly, the v0.3 follow-up ADR may demote only
   one of the two. This ADR's criterion is written on entity-coreference
   recall specifically; the synthesis half rides with it by default but can
   be decoupled if the data warrants.
4. **What if `hermes-lcm` later adds an entity subsystem?** The sibling
   shipping without one is a load-bearing data-point for S7. If `hermes-lcm`
   adds entity coreference before Epic-09's eval runs, that weakens the
   "deferrable for a minimal product" half of the rationale. Re-check the
   sibling's surface when the criterion is evaluated; this does not change the
   keep-decision (which rests on ADR-030), only the strength of the case for
   the demotion ramp.
