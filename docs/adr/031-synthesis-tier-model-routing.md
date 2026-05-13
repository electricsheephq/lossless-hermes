# ADR-031: Synthesis tier-to-model routing policy

**Status:** Accepted
**Date:** 2026-05-14
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

LCM v4.1's `src/synthesis/dispatch.ts` (commit `1f07fbd` on branch `pr-613`) routes every tier
through one model. The model name is read from a single environment variable
`LCM_SUMMARY_MODEL` with a `"gpt-5.4-mini"` fallback, and the per-tier defaults map at
`dispatch.ts:71-79` populates **every tier** with that same value:

```ts
const _LCM_DEFAULT_MODEL = process.env.LCM_SUMMARY_MODEL?.trim() || "gpt-5.4-mini";
export const DEFAULT_MODEL_BY_TIER: Record<TierLabel, string> = {
  daily: _LCM_DEFAULT_MODEL, weekly: _LCM_DEFAULT_MODEL,
  monthly: _LCM_DEFAULT_MODEL, yearly: _LCM_DEFAULT_MODEL,
  custom: _LCM_DEFAULT_MODEL, filtered: _LCM_DEFAULT_MODEL,
};
```

The seed in `src/synthesis/seed-default-prompts.ts` leaves every row's `model_recommendation`
column NULL — there is no opinionated haiku → sonnet → opus ladder in the seeded data.
Tier-specific tuning is expected to be done by operators editing `model_recommendation`
on individual `lcm_prompt_registry` rows.

The constraint forcing a choice for the Python port: should we replicate that "one env
var, everything on one model" stance, or seed an opinionated tier ladder so the
out-of-box behaviour is "haiku for daily, sonnet for weekly/monthly/custom/filtered,
opus + extended thinking for yearly best-of-N"?

`docs/porting-guides/synthesis.md` §"Open decisions" §1 raised this as ADR-?:

| Tier | Pass strategy | Recommended default (porting guide) | Use case | Latency | Cost ratio |
|---|---|---|---|---|---|
| daily | single | claude-3-5-haiku | leaf → daily | <2s | 1x |
| weekly | single | claude-sonnet-4 | daily → weekly | 3-6s | 5x |
| monthly | single + verify_fidelity | claude-sonnet-4 | weekly → monthly | 5-10s | 7x (2 calls) |
| yearly | best_of_n (3) + judge | claude-opus-4 + extended thinking | monthly → yearly | 30-60s | 40x |
| custom | single | claude-sonnet-4 | `lcm_synthesize_around` | 3-6s | 5x |
| filtered | single | claude-sonnet-4 | grep-filtered window | 3-6s | 5x |

We need to decide what ships in v0.1.

## Options considered

### Option A: Match TS exactly (single env var, NULL `model_recommendation` in seed)

- **Description:** Carry the TS contract verbatim. `DEFAULT_MODEL_BY_TIER` reads
  `LCM_SUMMARY_MODEL` at module load with a `"gpt-5.4-mini"` fallback, populates every
  tier with that single value, and `seed_prompts.py` leaves every row's
  `model_recommendation` field as `None`. Per-prompt tuning remains operator-driven via
  `register_prompt(..., model_recommendation="…")` (and the same call site is the
  override surface for B/C in the future).
- **Pros:**
  - **Maximum simplicity.** One environment variable, one knob. The operator's existing
    `summarize.ts` / leaf-summarizer convention uses the same `LCM_SUMMARY_MODEL` name —
    dispatch follows suit so the operator's model knob lives in one place.
  - **No bake-in risk.** Anthropic's model naming has changed twice in the last six
    months (Sonnet 3.5 → Sonnet 4; Opus 3 → Opus 4). Not committing a specific name to
    seed text means a v0.2 model-rename does not require a seed migration.
  - **Deferral cleanly aligns with Epic 09 eval.** The porting guide's recommendation is
    explicitly "Probably C, but defer until benchmarking spike runs on real Hermes
    traffic" (`synthesis.md` §"Open decisions" §1). Epic 09 builds that benchmarking
    surface; v0.2 (or later) can revisit when there's data.
  - **Behavioural parity with TS is mandatory test coverage** anyway. `force_model`
    precedence (Wave-4 Auditor #5 P1) is item 2 on the §"Behavioral parity checklist"
    — that test passes only if A is chosen (or if B/C is chosen AND the precedence
    matrix preserves "force_model → tier default, NOT prompt recommendation").
- **Cons:**
  - **Out-of-box behaviour is "everything runs on one model."** Yearly synthesis wastes
    compute on a haiku-class model; daily wastes money on an opus-class model. The
    operator must learn the override surface to fix this. Mitigation: documentation —
    the override path is one `register_prompt(... model_recommendation="…")` call.
  - **Cost-table coupling at audit time.** If the operator later sets
    `model_recommendation = "claude-opus-4"` on yearly + best_of_n_judge, the audit
    layer's `cost_usd_cents` math (07-09) must already know that model's per-token
    rate. We accept this as a 07-09-owned concern; no v0.1 work needed.

### Option B: Seed the opinionated ladder (haiku / sonnet / opus + thinking)

- **Description:** Update `seed_prompts.py` so every default row has a non-NULL
  `model_recommendation`: daily → haiku, weekly/monthly/custom/filtered → sonnet,
  yearly (single + best_of_n_judge) → opus with extended thinking. Wire a thinking-mode
  knob in Epic 04's LLM adapter.
- **Pros:**
  - **Best out-of-box defaults.** First-run operators see tier-appropriate spend +
    latency without learning the override surface.
  - **Eliminates "one model for everything" foot-gun** for operators who set
    `LCM_SUMMARY_MODEL=gpt-5.4-mini` for daily and forget to bump for yearly.
- **Cons:**
  - **Bakes Anthropic model names into seed text.** Future model renames trigger seed
    migration; v0.2 stub-tier migration (ADR-030) shows this is non-trivial.
  - **Couples synthesis to Epic 04 adapter** — extended-thinking requires a new
    `thinking={"type": "enabled", "budget_tokens": …}` knob in the adapter. Tracking
    issue would need to land before this seed can be exercised end-to-end.
  - **Pre-empts Epic 09 eval data.** The porting guide flagged "defer until
    benchmarking spike runs" — choosing now is speculative.
  - **Cost-table coupling becomes mandatory.** 07-09 audit rate table must include all
    four tiers' models on day one (vs. "one model, one rate" in A).

### Option C: Hybrid — env var as global default, seed `model_recommendation` for yearly only

- **Description:** Keep TS's env var as the global default for daily/weekly/monthly/
  custom/filtered (every tier-default in `DEFAULT_MODEL_BY_TIER` reads
  `LCM_SUMMARY_MODEL`). Seed the yearly + best_of_n_judge row with a specific
  opus-class model + extended thinking — that pass is expensive enough (40x cost
  ratio, 30-60s latency, 4 LLM calls) that downgrading to a smaller model is a clear
  win the operator should not have to discover.
- **Pros:**
  - **Targeted optimization where it matters.** Yearly best-of-N is the only place the
    "everything on one model" foot-gun has order-of-magnitude consequences.
  - **Lowest bake-in surface.** Only one model name in seed text, in only one row
    (yearly best_of_n_judge). v0.2 model rename touches one row.
- **Cons:**
  - **Still bakes one Anthropic model name** into the seed; the bake-in concern from B
    applies, just smaller in scope.
  - **Requires extended-thinking adapter knob** (same as B) — yearly judge needs a
    thinking-mode call to be useful, otherwise we're paying for opus without using its
    differentiating feature.
  - **Pre-empts Epic 09 eval data** for the one place that matters most. Counter-
    argument: yearly is rare enough that "wait for eval" is acceptable.

## Decision

Chosen: **Option A — match TS exactly. Ship NULL `model_recommendation` in every seeded
row, route every tier through `LCM_SUMMARY_MODEL` (fallback `"gpt-5.4-mini"`), defer the
opinionated ladder until Epic 09 eval data exists.**

This is the ROADMAP-aligned default. The 07-10 issue spec recommends shipping Option A
for v0.1 explicitly: "the cost difference at v0.1-traffic levels is negligible; the
eval data from Epic 09 will inform whether to upgrade to B or C in v0.2."

## Rationale

1. **The porting guide's recommendation is "probably C, but defer"** — that "defer" is
   load-bearing. Without Epic 09 traffic data, we'd be choosing model names for our
   tier ladder based on the porting guide author's hunch, not measured behaviour.
   Option A is the only choice that respects the deferral.

2. **Bake-in risk is real.** Anthropic's model naming has churned twice in six months.
   `claude-3-5-haiku` and `claude-sonnet-4` and `claude-opus-4` are all current as of
   2026-05-14, but a v0.2 release in three months may need to migrate the names. The
   seed-data migration path is non-trivial (operator may have overridden some rows; we
   can't blindly UPDATE every default). A seed that bakes specific names creates work
   we don't yet have evidence we need.

3. **The override surface is already complete.** Per
   `src/lossless_hermes/synthesis/prompt_registry.py::register_prompt`, an operator who
   wants tier-specific routing today writes:

   ```python
   register_prompt(
       db,
       RegisterPromptOptions(
           memory_type="episodic-yearly",
           tier_label="yearly",
           pass_kind="best_of_n_judge",
           template=existing_template,
           model_recommendation="claude-opus-4",  # operator chooses
       ),
   )
   ```

   No new code is needed for B or C operators — the registry already gives them the
   knob. We're choosing whether to ship opinionated defaults, not whether to enable
   the operator's path.

4. **`force_model` precedence stays unambiguous.** Item 2 on
   `synthesis.md` §"Behavioral parity checklist" (Wave-4 Auditor #5 P1) requires
   `force_model=True` without `model_override` to fall through to
   `DEFAULT_MODEL_BY_TIER[tier]` — NOT the prompt's `model_recommendation`. With A,
   that fall-through is `LCM_SUMMARY_MODEL` regardless of tier, which is exactly the
   TS behaviour the test pins.

5. **Cost-table burden stays minimal.** 07-09's audit `cost_usd_cents` math needs one
   per-model rate (whatever `LCM_SUMMARY_MODEL` resolves to). With B or C, it needs
   four. We can defer the multi-model rate table to v0.2 alongside the seed update.

## Consequences

- **`src/lossless_hermes/synthesis/seed_prompts.py`** ships with
  `model_recommendation=None` on every row — already true at the v0.1 cut. No code
  change required.

- **`src/lossless_hermes/synthesis/dispatch.py`** keeps `_pick_model`'s precedence
  matrix unchanged. The precedence is preserved verbatim from TS
  (`dispatch.ts:755-766`):

  1. `force_model=True` and `model_override` set → `model_override`
  2. `force_model=True` alone → `DEFAULT_MODEL_BY_TIER[req.tier]`
  3. otherwise: `prompt.model_recommendation` or `model_override` or
     `DEFAULT_MODEL_BY_TIER[req.tier]`

- **`src/lossless_hermes/synthesis/tier_routing.py`** (NEW) exposes the routing policy
  as a public, audit-friendly surface: `resolve_default_model_from_env`,
  `pick_synthesis_model`, plus a `TIER_LADDER_DEFERRED` constant referencing this ADR.
  The module wraps `dispatch.py`'s internals so callers (e.g. health-check, /lcm
  status, future tier-policy override surfaces) can read the routing state without
  importing `_pick_model` (a private API).

- **Override surface for operators is `register_prompt(...)`** with a non-None
  `model_recommendation`. Same path is used for Option B/C migration if/when v0.2
  flips the policy: change the seed table + write a one-time `bump_bundle_version`
  migration (the operator's override row stays untouched because of the v1-then-skip
  idempotency in `seed_default_prompts`).

- **`docs/porting-guides/synthesis.md` §"Open decisions" §1** is updated:
  status flips from `OPEN` → `RESOLVED → ADR-031`.

- **`epics/07-entity-synthesis/README.md`** Open Decision A note flips from "85%
  confidence driver" to "resolved per ADR-031".

- **Behavioral parity test** in `tests/synthesis/test_dispatch.py::TestParityChecklist`
  `test_2_force_model_no_override_uses_tier_default` continues to pass without change.
  The test pins the `force_model` precedence — the precedence is the same regardless
  of which option ships.

- **Epic 09 eval is the trigger** to revisit. If Epic 09's recall benchmarks show that
  yearly best-of-N quality is materially lower with `LCM_SUMMARY_MODEL` defaulted to
  a small model than with a seeded opus + thinking ladder, that data lands a follow-up
  ADR (32 or higher) flipping to Option B or C and updating the seed.

- **Invariant:** if a future ADR flips to B or C, the precedence matrix in `_pick_model`
  does **not** change. The `force_model` semantics are load-bearing for the parity
  checklist; we add seed data, we do not change the resolver.

## Open questions / 5% uncertainty

1. **What if `LCM_SUMMARY_MODEL` ships unset on a fresh install?** Fallback is
   `"gpt-5.4-mini"`. That's a non-Anthropic model name in a port that targets the
   Anthropic SDK in Epic 04. Two possibilities:
   - Operator sets `LCM_SUMMARY_MODEL` before first call — preferred. README should
     call this out as a v0.1 install step.
   - Operator does not set it — `"gpt-5.4-mini"` is dispatched as the model name. The
     Hermes-side adapter (Epic 04) decides what to do: pass through to the SDK and
     fail with a clear error, or fall back to a known-good default. This is an Epic 04
     concern, not a 07-10 one.

   Mitigation: `tier_routing.py` exposes `resolve_default_model_from_env()` so /lcm
   health (08-03) can surface the resolved name in operator output — they see
   immediately whether their env var landed.

2. **What if a future Hermes-side adapter needs per-tier model strings to be valid
   Anthropic model IDs at module load?** The TS source reads the env at module load
   too — same risk. Mitigation: validation lives in the adapter (Epic 04), not in
   `tier_routing`. The routing module stays vendor-agnostic.

3. **Should the deferred-decision marker be a code constant or just a doc string?**
   Chose code constant `TIER_LADDER_DEFERRED` in `tier_routing.py` so a `grep -rn
   "TIER_LADDER_DEFERRED" src/lossless_hermes/` enumerates the deferral marker the
   same way `grep -rn "# LCM Wave-"` enumerates Wave-N markers (per ADR-029). The
   constant carries a docstring pointing at this ADR for forward-reference clarity.

4. **Should we ship a feature-flag environment variable to opt into B/C early?**
   No. The override surface is `register_prompt(model_recommendation=…)` — already
   present, already tested. A feature flag would create a second code path for the
   same behaviour. If an early-adopter operator wants the tier ladder before v0.2,
   they write 4-5 `register_prompt` calls in their `lossless-hermes` plugin init.
