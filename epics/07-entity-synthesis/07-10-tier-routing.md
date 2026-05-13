---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] synthesis: tier-to-model routing seed (ADR-? Open Decision A)'
labels: 'port, epic-07, adr-pending, deferred-decision'
---

## Source (TypeScript)

- **No TS counterpart.** This is a Hermes-side recommendation tracked as Open Decision A in `docs/porting-guides/synthesis.md` §"Open decisions" §1. The TS source has all `model_recommendation = NULL` in the seed; every tier defaults to one global env var `LCM_SUMMARY_MODEL` (fallback `"gpt-5.4-mini"`).

## Target (Python)

- File: `src/lossless_hermes/synthesis/seed_prompts.py` (the `model_recommendation` column values in the 10–11 seeded rows from 07-08)
- File: `docs/adr/0XX-synthesis-tier-model-routing.md` (new ADR; number assigned at PR time)
- Estimated LOC: ~20 LOC of model-name strings + ~200 LOC of ADR text

## What this issue covers

The decision and its implementation: **should the Python port seed an opinionated haiku→sonnet→opus→opus-thinking ladder via `model_recommendation`, or match TS exactly with all NULLs?**

Per `synthesis.md` "Tier model" section, the recommended Hermes-side ladder is:

| Tier | Pass strategy | Recommended default model | Use case | Latency | Cost ratio |
|---|---|---|---|---|---|
| daily | single | claude-3-5-haiku | leaf → daily condensation | <2s | 1× |
| weekly | single | claude-sonnet-4 | daily → weekly | 3–6s | 5× |
| monthly | single + verify_fidelity | claude-sonnet-4 | weekly → monthly, hallucination-checked | 5–10s | 7× (2 calls) |
| yearly | best_of_n (3) + judge | claude-opus-4 + extended thinking | monthly → yearly (4 calls total) | 30–60s | 40× |
| custom | single | claude-sonnet-4 | ad-hoc `lcm_synthesize_around` time/semantic windows | 3–6s | 5× |
| filtered | single | claude-sonnet-4 | ad-hoc `lcm_synthesize_around` grep-filtered | 3–6s | 5× |

The three options from `synthesis.md` §"ADR-?: Tier-to-model mapping policy":

- **Option A — Match TS exactly.** Single env var `LCM_SUMMARY_MODEL`, NULL `model_recommendation` in seed. Pros: maximum simplicity, one knob for operators. Cons: out-of-box behavior is "everything runs on one model" — yearly synthesis wastes compute on haiku, daily wastes money on opus.
- **Option B — Seed the ladder.** Pros: better out-of-box. Cons: requires operator to learn the override surface to change it; couples the port to Anthropic model names baked in seed text.
- **Option C — Hybrid.** Keep TS's env var as the global default, but seed `model_recommendation` for `yearly + best_of_n_judge` only (where best-of-N is expensive enough that downgrading to a smaller model is a clear win).

**Recommendation (porting-guide):** Probably C, but defer until benchmarking spike runs on real Hermes traffic.

**Recommendation (this issue):** **Ship Option A** for v0.1. Match TS exactly. The cost difference at v0.1-traffic levels is negligible; the eval data from Epic 09 will inform whether to upgrade to B or C in v0.2. Avoid baking Anthropic model names into seed text before we have empirical evidence they're the right choices.

If shipping Option A: this issue is a no-op at the code level (`model_recommendation = NULL` in all 10–11 seeded rows from 07-08 — already covered there). The remaining deliverable is the ADR document.

If shipping Option B or C: this issue updates the seed in `seed_prompts.py` AND wires extended-thinking support in the LLM adapter (Epic 04).

## Dependencies

- Depends on: 07-08 (the seed module whose `model_recommendation` column this potentially writes), Epic 04 (LLM adapter — extended-thinking knob if B/C chosen)
- Blocks: nothing in this epic (terminal node; Epic 09 eval may revisit)

## Acceptance criteria

- [ ] ADR drafted at `docs/adr/0XX-synthesis-tier-model-routing.md` following the template at `docs/adr/000-template.md`; status `Proposed` at PR time, `Accepted` when merged
- [ ] ADR enumerates Options A, B, C with their pros/cons from `synthesis.md` §"Open decisions"
- [ ] ADR records the chosen option with rationale; if A is chosen, calls out the explicit deferral to Epic 09 eval before reconsidering
- [ ] ADR documents the override surface either way: operator updates `model_recommendation` via `register_prompt(...)` from `prompt_registry.py` (07-08)
- [ ] If Option B or C chosen: `seed_prompts.py` updated with the appropriate `model_recommendation` strings; extended-thinking adapter knob lands in Epic 04 with a tracking issue cross-referenced here
- [ ] Update `docs/porting-guides/synthesis.md` §"Open decisions" §1 status from `OPEN` to `RESOLVED → ADR-0XX`
- [ ] Update `epics/07-entity-synthesis/README.md` confidence note (currently calls Open Decision A as one of two 85%-confidence drivers — flip to "resolved" or "still deferred per ADR-0XX")
- [ ] Cross-reference `synthesis.md` Behavioral parity checklist item 2 (force_model precedence) — the test there does NOT change regardless of which option is chosen
- [ ] No code regressions in 07-05 / 07-08 / 07-09 (test suite stays green)

## Tests to port

None — this is a documentation + seed-data issue. The behavioral parity tests from 07-05 already cover the `_pick_model` precedence matrix. If Option B/C chosen:

| New test | Cases |
|---|---|
| `tests/synthesis/test_tier_ladder.py` | (1) seeded `yearly + best_of_n_judge` has non-NULL `model_recommendation`; (2) `_pick_model` falls through to that recommendation when `force_model=False` and no `model_override`; (3) extended-thinking adapter knob set when model name matches the opus-thinking pattern |

## Estimated effort

**2–4 hours.** Most cost is in writing the ADR (the analysis is already done in `synthesis.md`; this is mostly transcription + decision). If Option A: ~2 h (ADR + cross-references). If Option B/C: ~4 h (seed changes + adapter wiring + new test cases).

## Confidence

**70%.** This is the lowest-confidence issue in the epic because:

- **The decision itself is deferred until Epic 09 eval data exists.** The porting guide's recommendation is "probably C, but defer". Shipping anything other than A in v0.1 is speculative.
- **Model names drift fast.** Anthropic's naming has changed twice in the last 6 months. Baking `claude-3-5-haiku` into seed text means a v0.2 model-rename triggers a seed migration.
- **Cost-table coupling** — if B/C is chosen, the LLM adapter's per-model rate table (07-09 audit, `cost_usd_cents`) must include all four tiers' models. If the rate table is stale, audit cost numbers drift silently.

The 70% reflects "we know what we'd ship but the decision is the open part." If user picks A explicitly, confidence flips to 95%. If user picks B/C, confidence flips to 80% (with the new-surface risks above).
