# ADR-033: Embeddings default posture — opt-in / off by default

**Status:** Accepted
**Date:** 2026-05-19
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

> **Implementation is v0.2.0-tracked.** This ADR records the *decision* reached by
> the `hermes-lcm` architecture review at ≥95% confidence. The code changes
> (flipping `hybrid`/`semantic` to opt-in, rewording the `lcm_grep` tool-schema
> prose) are scheduled for v0.2.0 — see
> [GitHub issue #133](https://github.com/electricsheephq/lossless-hermes/issues/133).
> The **keep-vs-cut decision for the embeddings stack itself stays open** (see
> §Open questions) — only the *default posture* is decided here.

## Context

`lossless-hermes` v0.1.0 ships a Voyage-embeddings retrieval stack: `lcm_grep`'s
`hybrid` and `semantic` modes, the `src/lossless_hermes/embeddings/` package
(`semantic_search.py`, `hybrid_search.py`, the Voyage client, the vec0 KNN store,
the backfill worker) — roughly **~3,500 LOC** across source and tests. Two project
positions, set during the port, are now in question:

1. **The `lcm_grep` tool-schema prose advertises `hybrid` as "PRIMARY".** The
   verbatim tool description (`docs/porting-guides/tools.md` §`lcm_grep`, the
   `description` field the agent actually reads) says:

   > `hybrid` — FTS5 + Voyage semantic + rerank (**PRIMARY for Type B
   > topic-anchored queries**: 'have we ever discussed X', 'what work has been done
   > on Y' — handles paraphrases like 'merge mess' → 'rebase blew up')

   The agent is told, in the tool schema, that hybrid is the *primary* path for an
   entire class of queries.

2. **The headline justification for the embeddings stack is a "+52.5pp paraphrastic
   recall lift."** This number appears in `docs/related-work.md`, in
   `docs/porting-guides/embeddings.md` ("Empirically +52.5pp lift over FTS-only on
   Eva's 31-query paraphrastic eval"), and as the entire premise of the Epic-09
   benchmark `docs/benchmarks/voyage-recall-2026-q2.md`.

The `hermes-lcm` architecture review examined both and found them unsupported by any
measurement *in this repo*:

- **The +52.5pp number was never measured here.** It is a *TS-baseline target*
  carried over from the LCM Phase-A spike against Eva's private snapshot DB — not a
  Python result. `docs/benchmarks/voyage-recall-2026-q2.md` is explicit and
  repeated about this:

  > **Status: HYBRID ARM PENDING.** ... the `hybrid` arm ... requires a live
  > `VOYAGE_API_KEY`, which was not available in the run environment.

  > The +52.5pp number below is the **TS-baseline target**, NOT a measured Python
  > result — it must not be cited as reproduced until the run below completes.

  The benchmark's `Py Hybrid` and `Py lift` columns are literally `_pending_`. The
  hybrid arm of the Python benchmark **never ran** — no `VOYAGE_API_KEY` was
  provisioned (this is also `BLOCKERS.md` item B-001). Only the FTS-only baseline
  was measured; on the synthetic corpus that baseline's paraphrastic recall is
  **0.0%**. The "+52.5pp lift" is the gap between a measured 0% FTS floor and an
  *unmeasured, assumed* hybrid number.

- **Hybrid hard-fails without a key, so it is a no-op for the keyless majority.**
  `lcm_grep` with `mode='hybrid'` and no `VOYAGE_API_KEY` returns an error:
  `"...hybrid mode requires it. Use mode='full_text' for keyword-only search."`
  (`docs/porting-guides/tools.md` line 117). The semantic-vec0 path likewise raises
  `SemanticSearchUnavailableError` when vec0 is absent. For any operator who has not
  configured a Voyage key — the expected majority of installs — the "PRIMARY"
  hybrid mode is not a primary path; it is an immediate failure that the agent must
  recover from by falling back to `full_text`.

So the project simultaneously (a) tells the agent in a tool schema that hybrid is
*primary*, and (b) ships a hybrid path that, for most installs, hard-fails on first
use, justified by (c) a recall lift that was never measured. The review (95%
confidence) flagged this triad as a correctness-of-claims problem.

The constraint forcing a choice: what is the **default posture** of the embeddings
modes for v0.2.0 — on by default and advertised as primary, or opt-in and off by
default?

> **Scope note.** This ADR decides *only the default posture*. It does **not**
> decide whether to keep or cut the ~3,500-LOC embeddings stack. That keep-vs-cut
> question is explicitly recorded as **open** in §Open questions and is gated on a
> real benchmark.

## Options considered

### Option A: Status quo — hybrid/semantic on, `lcm_grep` schema advertises hybrid as "PRIMARY"

- **Description:** Keep the current v0.1.0 behavior. `hybrid`/`semantic` are
  first-class modes; the tool schema continues to tell the agent hybrid is the
  primary path for topic-anchored ("Type B") queries.
- **Pros:**
  - No change; no v0.2.0 work.
  - If the +52.5pp lift turns out to be real and operators do configure Voyage
    keys, the agent is already steered toward the better path.
- **Cons:**
  - **The "PRIMARY" claim is unbacked.** No Python measurement supports steering the
    agent to hybrid. The +52.5pp number is a TS-baseline target, not a result
    (`docs/benchmarks/voyage-recall-2026-q2.md`, repeatedly).
  - **The advertised primary path hard-fails for the keyless majority.** An agent
    that follows the schema's "PRIMARY for Type B" guidance issues a `hybrid` call,
    gets an error, and must recover — every time, for every keyless install. The
    schema actively misleads the agent.
  - **Tool-schema prose is load-bearing.** The agent's tool-selection behavior is
    driven by that description text. Telling it to prefer a mode that will fail is a
    real behavior bug, not a doc nit.
- **Evidence cited:** `docs/porting-guides/tools.md` §`lcm_grep` (the "PRIMARY"
  prose, line 44; the keyless error, line 117); `docs/benchmarks/voyage-recall-2026-q2.md`
  (hybrid arm `_pending_`); `BLOCKERS.md` B-001.

### Option B: Opt-in / off by default — hybrid/semantic available but not default; schema stops advertising hybrid as "PRIMARY"

- **Description:**
  - `hybrid` and `semantic` retrieval modes become **opt-in and OFF by default.** An
    operator turns them on explicitly (a config flag and/or by provisioning a
    Voyage key — exact mechanism is a v0.2.0 design detail). With no opt-in, the
    modes are not offered.
  - The `lcm_grep` tool-schema `description` **stops advertising `hybrid` as
    "PRIMARY"** for Type B queries. The honest default steer for keyless installs is
    `full_text` (FTS5) followed by `lcm_describe` / `lcm_expand_query` drill-down.
    The schema describes `hybrid`/`semantic` as available *when embeddings are
    enabled*, without a primacy claim.
  - The ~3,500-LOC embeddings stack **stays in the tree, pending the real
    benchmark** (see Open questions). Off-by-default is not a deletion.
- **Pros:**
  - **The default matches what the keyless majority can actually run.** FTS5 +
    drill-down works with zero external dependencies; it is the path most installs
    will use. Making it the default steer removes the guaranteed first-call failure.
  - **The tool schema stops misleading the agent.** No "PRIMARY" claim that the
    measurement does not support; no steer toward a mode that hard-fails keyless.
  - **It does not pre-judge the keep-vs-cut question.** The stack stays; only its
    default posture flips. If the real benchmark later shows a large lift, flipping
    `hybrid` back to a recommended (even default-on-when-keyed) posture is a small
    schema + config change.
  - **Matches `hermes-lcm`'s posture.** `hermes-lcm` ships *without* the embeddings
    stack at all (`docs/related-work.md`: "a simpler shipping-today Hermes-LCM
    plugin without embeddings"); off-by-default brings `lossless-hermes`'s
    *out-of-box* behavior in line with the proven-sufficient FTS baseline while
    keeping the embeddings option for operators who want it.
  - **No claim is made that cannot be reproduced.** This is the core of the review's
    finding — every retrieval claim the project ships should be one a keyless
    operator can verify, or be clearly marked opt-in.
- **Cons:**
  - **If the +52.5pp lift is real, off-by-default leaves recall on the table for
    operators who never opt in.** Mitigation: the lift is *unmeasured*; we cannot
    spend a default on an unverified number. Operators who want it opt in. Once the
    benchmark runs, the default can be revisited.
  - **A capability the project built is no longer front-and-center.** Mitigation:
    the stack is not removed; documentation points operators with a Voyage key at
    the opt-in. The capability is preserved, just not the default.
  - **Two retrieval postures to document** (keyless default vs embeddings-enabled).
    Mitigation: this is inherent to an optional dependency; it is also already true
    today (the keyless error message *already* tells the agent to fall back).
- **Evidence cited:** `docs/benchmarks/voyage-recall-2026-q2.md` (FTS-only baseline
  is the only measured arm; hybrid arm never ran); `docs/related-work.md`
  (`hermes-lcm` ships without embeddings and is a 540★ production system);
  `hermes-lcm` architecture review (95%).

### Option C: Cut the embeddings stack entirely in v0.2.0

- **Description:** Delete the ~3,500-LOC embeddings stack (`hybrid`/`semantic`
  modes, the Voyage client, vec0, the backfill worker). Ship FTS-only, like
  `hermes-lcm`.
- **Pros:**
  - Largest reduction in surface area, dependencies, and maintenance.
  - Removes the unbacked-claim problem at the root.
- **Cons:**
  - **Pre-judges a question that has not been measured.** If the real
    hybrid-vs-FTS+drill-down benchmark shows a large, reproducible lift, deleting the
    stack would have thrown away a genuine differentiator. The decision to cut must
    be made *with* the benchmark number, not before it.
  - **Discards work and a stated differentiator.** `docs/related-work.md` positions
    Voyage embeddings + hybrid search as a deliberate differentiator vs `hermes-lcm`.
    Cutting it is a strategic reversal that should not be made on absence-of-evidence
    alone.
  - **Irreversible at low cost.** Re-deleting later is cheap; re-adding ~3,500 LOC
    later is not. Keeping the stack pending the benchmark preserves optionality;
    cutting now destroys it.
- **Evidence cited:** `docs/related-work.md` (embeddings positioned as a
  differentiator); the keep-vs-cut question is, by the review's own framing, gated
  on a measurement that does not yet exist.

## Decision

Chosen: **Option B — `hybrid`/`semantic` retrieval modes become opt-in and OFF by
default; the `lcm_grep` tool-schema prose stops advertising `hybrid` as "PRIMARY".**

The default retrieval posture for v0.2.0 is FTS5 (`full_text`) + drill-down
(`lcm_describe` / `lcm_expand_query`) — the path every install can run with zero
external dependencies. `hybrid` and `semantic` remain in the tree as an opt-in
capability for operators who provision a Voyage key.

**The keep-vs-cut decision for the ~3,500-LOC embeddings stack is NOT made here. It
stays open**, gated on the real hybrid-vs-FTS+drill-down benchmark (Open question 1).
This ADR's `Accepted` status applies to the **default-off / no-"PRIMARY"-claim
decision**; it does not endorse, and does not preclude, a future decision to keep or
cut the stack.

## Rationale

1. **A tool schema must not tell the agent to prefer a path the measurement does not
   support.** The `lcm_grep` `description` field is read by the agent and drives its
   mode selection. Calling `hybrid` "PRIMARY for Type B queries" is a *steering*
   instruction. The only evidence cited for that steer — +52.5pp — is, per the
   project's own benchmark doc, a TS-baseline target with the Python hybrid arm
   `_pending_`. Steering the agent on an unmeasured number is the unbacked-claim
   problem the review flagged at 95%. Removing the "PRIMARY" word is the minimal
   honest correction.

2. **The advertised primary path hard-fails for the keyless majority — that is a
   behavior bug.** With no `VOYAGE_API_KEY`, `mode='hybrid'` returns an error
   (`docs/porting-guides/tools.md` line 117) and `semantic` raises
   `SemanticSearchUnavailableError`. An agent that obeys the schema's "PRIMARY"
   guidance therefore *fails on first use* on every keyless install, then recovers
   to `full_text`. The default should be the path that works, not the path that
   fails-then-recovers. Off-by-default makes the working path the default.

3. **The +52.5pp lift was never measured in this repo.** This is not a hedge — the
   benchmark doc states it four times (`docs/benchmarks/voyage-recall-2026-q2.md`
   lines 9, 23, 103; `BLOCKERS.md` B-001). The Python hybrid benchmark arm did not
   run because no key was provisioned. The "+52.5pp" is the gap between a *measured*
   0.0% FTS-only paraphrastic floor and an *assumed* hybrid number inherited from a
   TS spike against a private corpus. A default posture cannot rest on an inherited,
   unreproduced number.

4. **Off-by-default does not pre-judge keep-vs-cut — it preserves optionality.**
   Option C (cut now) would destroy ~3,500 LOC on absence of evidence; if the
   benchmark later shows a real lift, that is unrecoverable at low cost. Option B
   keeps the stack, flips only the default, and leaves the keep-vs-cut decision for
   when the number exists. This is the reversible choice: flipping the default back
   on (when keyed) is a small change; re-adding a deleted stack is not.

5. **Off-by-default aligns the out-of-box behavior with a proven baseline.**
   `hermes-lcm` (540★, production) ships *without* an embeddings stack at all and is
   a fully functional Hermes-LCM plugin (`docs/related-work.md`). FTS5 + drill-down
   is therefore a *proven-sufficient* default. `lossless-hermes` keeps the
   embeddings option that `hermes-lcm` lacks — but as an opt-in, not as an unverified
   default.

6. **Every retrieval claim the project ships should be reproducible by a keyless
   operator, or clearly marked opt-in.** This is the review's underlying principle.
   The FTS-only baseline is reproducible offline by anyone
   (`docs/benchmarks/voyage-recall-2026-q2.md` §"Measured FTS-only baseline"). The
   hybrid claim is not. So FTS is the default and the honest steer; hybrid is opt-in
   and carries no primacy claim until a reproducible number backs it.

## Consequences

- **`lcm_grep` tool-schema prose is reworded (v0.2.0).** The verbatim `description`
  string in the tool definition (sourced from `docs/porting-guides/tools.md`
  §`lcm_grep`) is edited so that:
  - the word **"PRIMARY"** is removed from the `hybrid` mode clause;
  - `hybrid`/`semantic` are described as available **when embeddings are enabled**,
    with no primacy claim for Type B queries;
  - the honest default steer for topic-anchored queries on a keyless install —
    `full_text` then `lcm_describe`/`lcm_expand_query` drill-down — is what the
    schema presents as the standard path.
  This is a behavior change: it changes how the agent selects retrieval modes.

- **`hybrid` and `semantic` become opt-in / OFF by default (v0.2.0).** With no
  opt-in, the modes are not offered as standard. The exact opt-in mechanism (a
  config flag such as `embeddings.enabled`, gating on a resolved `VOYAGE_API_KEY`,
  or both) is a v0.2.0 design detail — issue #133. The keyless hard-fail behavior
  (`docs/porting-guides/tools.md` line 117) is no longer something the agent reaches
  by default, because it is no longer steered toward `hybrid` by default.

- **The ~3,500-LOC embeddings stack stays in the tree.** `src/lossless_hermes/embeddings/`,
  the Voyage client, the vec0 KNN store, and the backfill worker are **not removed**
  by this ADR. Off-by-default is a posture change, not a deletion. The stack remains
  pending the real benchmark (Open question 1).

- **Docs that cite "+52.5pp" as if measured are corrected (v0.2.0).** Anywhere the
  +52.5pp lift is stated without the "TS-baseline target, not a Python result"
  qualifier — notably `docs/related-work.md` and `docs/porting-guides/embeddings.md`
  line 20 ("Empirically +52.5pp lift over FTS-only") — is updated to mark it as an
  *unreproduced TS-baseline target*. `docs/benchmarks/voyage-recall-2026-q2.md`
  already states this correctly and needs no change.

- **`BLOCKERS.md` B-001 is reframed.** B-001 currently tracks the +52.5pp benchmark
  as gated on an unprovisioned `VOYAGE_API_KEY`. Under this ADR the benchmark is no
  longer a *release* blocker (the default no longer depends on hybrid); it becomes
  the input to the still-open keep-vs-cut decision (Open question 1).

- **ADR-022 (Voyage credential resolution) is unaffected.** The three-tier key
  resolver still works; it simply resolves a key only for operators who opt in.

- **Operators who want hybrid retrieval have a documented opt-in path.** v0.2.0 docs
  explain how to enable embeddings (provision a Voyage key per ADR-022, set the
  opt-in flag). The capability is preserved for operators who want it.

- **Invariant — no retrieval mode is advertised as "primary" / "recommended" in the
  tool schema without a reproducible measurement behind it.** If a future ADR
  re-promotes `hybrid` (e.g. after the benchmark), the schema prose may say so *only
  with the benchmark number cited*. Until then, the schema is posture-neutral about
  `hybrid`/`semantic`.

- **Invariant — the keyless FTS path is always functional and always the default.**
  `full_text`, `regex`, `verbatim`, and the `lcm_describe`/`lcm_expand_query`
  drill-down chain require no external dependency and remain the default retrieval
  surface regardless of the keep-vs-cut outcome.

- **v0.1.0 ships unchanged.** v0.1.0 is already released; this ADR scopes v0.2.0
  work (issue #133). The v0.1.0 schema's "PRIMARY" wording is a known v0.1.0 issue,
  corrected by the #133 work.

## Open questions / 5% uncertainty

1. **(EXPLICITLY OPEN — NOT DECIDED HERE) Keep vs cut the ~3,500-LOC embeddings
   stack.** This ADR decides *default posture only*. Whether the stack is
   ultimately kept or removed is **open** and gated on a real measurement:

   > The genuine **hybrid-vs-(FTS + drill-down)** benchmark must run — hybrid arm
   > live, against a representative corpus — **before any "hybrid primary" claim is
   > made and before the keep-vs-cut decision is taken.**

   The comparison must be hybrid against the *actual keyless default path* (FTS5 +
   `lcm_describe`/`lcm_expand_query` drill-down), not hybrid against bare FTS5 — the
   drill-down chain is part of what a keyless agent does, and the honest baseline
   must include it. The run requires a provisioned `VOYAGE_API_KEY`
   (`docs/benchmarks/voyage-recall-2026-q2.md` §"Live hybrid run PENDING" has the
   exact command). Until that number exists:
   - the embeddings stack stays in the tree (kept pending the result);
   - no ADR re-promotes `hybrid` to "primary."

   A follow-up ADR (≥035), written *after* the benchmark, decides keep-vs-cut and
   whether `hybrid` earns a recommended posture. If the lift is large and
   reproducible against the drill-down baseline → keep, and possibly default-on
   when a key is present. If it is small → the keep-vs-cut decision weighs ~3,500
   LOC of maintenance against a marginal gain.

2. **What is the precise opt-in mechanism?** A boolean config flag
   (`embeddings.enabled`), implicit gating on a resolved `VOYAGE_API_KEY` being
   present, or both (flag *and* key required). Both-required is the most explicit
   and avoids surprising an operator who set a Voyage key for another purpose.
   Decide in v0.2.0 design (#133).

3. **Does any in-tree path other than `lcm_grep` assume embeddings are on?**
   `lcm_synthesize_around` has a `window_kind='semantic'` mode that also requires
   Voyage + vec0 (`docs/porting-guides/tools.md` line 371, 386). Its
   keyless-error behavior is already structured. Confirm during v0.2.0 that flipping
   the global default to off is consistently surfaced across *all* embeddings-
   dependent surfaces, not just `lcm_grep` — the agent should get one coherent
   posture.

4. **Could off-by-default surprise an operator migrating from OpenClaw who used
   hybrid there?** An OpenClaw operator who relied on hybrid retrieval would, after
   migrating, find it off until they opt in. Mitigation: the v0.2.0 migration docs
   and `import-openclaw` notes call out the opt-in explicitly, so the change is
   visible rather than silent.
