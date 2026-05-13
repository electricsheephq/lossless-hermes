# Porting Guide: Synthesis Dispatch

**Source LOC:** ~1557 (`src/synthesis/`: dispatch 817 + prompt-registry 305 + seed-default-prompts 435)
**Python target LOC:** ~1500
**Confidence target:** 95%
**Estimated effort:** 16–24 hours
**Epic:** 07-entity-synthesis

## Architecture summary

`dispatch.ts` is the **tier-dispatched synthesis orchestrator** that sits between the agent tools (`lcm_synthesize_around`, cold rewrites in the worker) and the LLM. Given a `SynthesizeRequest` carrying a `tier` (daily / weekly / monthly / yearly / custom / filtered), a `memoryType` (`episodic-leaf`, `episodic-condensed`, `episodic-yearly`, `procedural-extract`, `entity-extract`, `theme-consolidation`), and the source text, it:

1. Looks up the **active** versioned prompt template from `lcm_prompt_registry` for `(memory_type, tier_label, pass_kind)`.
2. Picks a model: `req.forceModel ? (req.modelOverride ?? DEFAULT_BY_TIER) : (prompt.modelRecommendation ?? req.modelOverride ?? DEFAULT_BY_TIER)`.
3. Runs the **pass strategy** for the tier:
   - `daily / weekly / custom / filtered` → single pass
   - `monthly` → single pass + `verify_fidelity` (separate hallucination-check call)
   - `yearly` → `best_of_n` (N=3, hard-capped at 5) in parallel + `best_of_n_judge` pass picking the winner
4. Records every LLM call to `lcm_synthesis_audit` (insert `status='started'` before the call → UPDATE to `completed`/`failed` after, with latency + cost telemetry, all rows sharing one `pass_session_id`).
5. Returns `SynthesizeResult{ output, primaryPromptId, auditIds, totalLatencyMs, totalCostCents, hallucinationFlagged?, bestOfN? }`. Caller (typically `lcm-synthesize-around-tool.ts`) decides whether to write the output to `summaries.content` (cold rewrite) or `lcm_synthesis_cache` (ad-hoc/filtered/yearly).

Critically, the LLM call itself is **injected** as `LlmCall: (args) => Promise<LlmCallResult>` — dispatch is LLM-vendor-agnostic. Production wires it to OpenClaw's `pi-ai`; tests inject deterministic mocks; the **Hermes port wires it to Hermes's existing `agent/llm_client.py`**.

This module is deliberately kept OUTSIDE the existing `summarize.ts` (leaf compactor) flow because the design tradeoffs differ: leaf summarization is hot-path single-prompt; dispatch is cold-rewrite tier-aware multi-pass with verification. Architecture-v4.1 §3 + §11 explicitly chose single-pass over critique-revise (literature consensus says critique-revise underperforms for summarization), so the supported pass kinds are exactly: `single`, `verify_fidelity`, `best_of_n_judge` (no critique step).

## File mapping

| TS | Python |
|---|---|
| `src/synthesis/dispatch.ts` (817 LOC) | `src/lossless_hermes/synthesis/dispatch.py` |
| `src/synthesis/prompt-registry.ts` (305 LOC) | `src/lossless_hermes/synthesis/prompt_registry.py` |
| `src/synthesis/seed-default-prompts.ts` (435 LOC) | `src/lossless_hermes/synthesis/seed_prompts.py` |

## Tier model

**Important:** the TS source does NOT hardcode a haiku → sonnet → opus → opus-thinking ladder. All six tiers default to a single env var, `LCM_SUMMARY_MODEL` (fallback `"gpt-5.4-mini"`):

```ts
const _LCM_DEFAULT_MODEL = process.env.LCM_SUMMARY_MODEL?.trim() || "gpt-5.4-mini";
export const DEFAULT_MODEL_BY_TIER: Record<TierLabel, string> = {
  daily: _LCM_DEFAULT_MODEL, weekly: _LCM_DEFAULT_MODEL,
  monthly: _LCM_DEFAULT_MODEL, yearly: _LCM_DEFAULT_MODEL,
  custom: _LCM_DEFAULT_MODEL, filtered: _LCM_DEFAULT_MODEL,
};
```

Tier-specific model tuning is done by setting `model_recommendation` per-row in `lcm_prompt_registry`, NOT by hardcoded ladders. The reason: the operator's existing leaf-summarizer (`summarize.ts`) uses the same `LCM_SUMMARY_MODEL` convention; dispatch follows suit to keep the operator's model knob in one place. The "tier model" is therefore really a **pass-strategy** decision; the model knob is per-prompt-row + per-call override.

Where the haiku → opus ladder would surface in the proposed Hermes port: in the **seeded prompts'** `model_recommendation` column (currently all NULL in the TS seed — see `seed-default-prompts.ts`). The port may want to bake an opinionated default ladder into the seed so callers don't need an external knob:

| Tier | Pass strategy | Recommended default model (Hermes-side) | Use case | Latency | Cost ratio |
|---|---|---|---|---|---|
| daily | single | claude-3-5-haiku | leaf → daily condensation | <2s | 1× |
| weekly | single | claude-sonnet-4 | daily → weekly | 3–6s | 5× |
| monthly | single + verify_fidelity | claude-sonnet-4 | weekly → monthly, hallucination-checked | 5–10s | 7× (2 calls) |
| yearly | best_of_n (3) + judge | claude-opus-4 + extended thinking | monthly → yearly (4 calls total) | 30–60s | 40× |
| custom | single | claude-sonnet-4 | ad-hoc `lcm_synthesize_around` time/semantic windows | 3–6s | 5× |
| filtered | single | claude-sonnet-4 | ad-hoc `lcm_synthesize_around` grep-filtered | 3–6s | 5× |

**Who decides the tier?** The caller. `lcm_synthesize_around` passes `tier: "custom"` or `"filtered"` depending on whether a grep filter was applied. Cold-rewrite workers (orchestrator) pass `tier: "daily" | "weekly" | "monthly" | "yearly"` by the rollup level. There is no auto-escalate logic — tier is fully caller-driven. (See ADR-? below.)

**Pass strategy enum** (`PassKind`, exported from prompt-registry.ts):

```ts
export type PassKind = "single" | "verify_fidelity" | "best_of_n_judge";
```

**Pass strategy by tier:**

```ts
export const PASS_STRATEGY_BY_TIER: Record<TierLabel, PassKind[]> = {
  daily:    ["single"],
  weekly:   ["single"],
  monthly:  ["single", "verify_fidelity"],
  yearly:   ["best_of_n_judge"], // expanded to N=3 single + 1 judge inside dispatch
  custom:   ["single"],
  filtered: ["single"],
};
```

## Cache key

The cache table is `lcm_synthesis_cache`. The PRIMARY KEY is `cache_id` (a random opaque string), but the **logical** uniqueness lives in a UNIQUE INDEX:

```sql
CREATE UNIQUE INDEX lcm_synthesis_cache_lookup_uniq
  ON lcm_synthesis_cache (
    session_key,
    range_start,
    range_end,
    leaf_fingerprint,
    COALESCE(grep_filter, ''),
    tier_label,
    prompt_id
  );
```

Field-by-field:

| Field | Type | Source | What it captures |
|---|---|---|---|
| `session_key` | TEXT | `conversations.session_key`, or `targetSummary.session_key`, or fallback `"agent:main:main"` | Multi-tenant isolation — caller A's cache never bleeds into caller B's. |
| `range_start` | TEXT (ISO 8601 UTC) | Window's start bound | Time window's left edge. For `period` mode it's the computed bound; for `time` mode `targetSummary.created_at - windowHours`; for `semantic` mode the earliest matched leaf's `created_at`. |
| `range_end` | TEXT (ISO 8601 UTC) | Window's end bound | Time window's right edge. |
| `leaf_fingerprint` | TEXT (24 hex chars) | `SHA-256(leafId_1 \0 leafId_2 \0 ...).slice(0, 24)` | Order-sensitive hash of the leaf summary_ids selected. Different leaf set → different fingerprint → fresh cache. Order matters (different ordering = different fingerprint). |
| `grep_filter` | TEXT (nullable) | The grep pattern when set, NULL otherwise | Differentiates "custom" (no filter) from "filtered" (with grep). NULL and `""` are unified via `COALESCE(grep_filter, '')` in the index. |
| `tier_label` | TEXT | `req.tier` | One of `'daily' \| 'weekly' \| 'monthly' \| 'yearly' \| 'custom' \| 'filtered'`. The CHECK was widened in `widenLcmSynthesisCacheTierCheck_v413` to admit all dispatch tier values. |
| `prompt_id` | TEXT FK → `lcm_prompt_registry` | Active prompt at synthesis time | When a prompt is updated (new version active), old cache rows become unreachable — fresh INSERT happens, old rows expire via age-based GC. |

**Wave-10 P1 fix to remember:** the original UNIQUE index keyed only on `(session_key, range_start, range_end, leaf_fingerprint, grep_filter)`. Adding `tier_label` and `prompt_id` was a correctness fix — without them, `tier='custom'` then `tier='filtered'` for the same leaf set collided, and active-prompt updates silently served stale text. Port MUST keep all 7 fields.

**Single-flight:** the lookup index makes `INSERT OR IGNORE` the cross-process latch. Caller A inserts with `status='building'`; caller B's `INSERT OR IGNORE` returns `changes=0` and SELECTs back to see A's in-flight row.

## Prompt registry

Schema (`lcm_prompt_registry`):

```sql
CREATE TABLE lcm_prompt_registry (
  prompt_id          TEXT NOT NULL PRIMARY KEY,
  memory_type        TEXT NOT NULL CHECK (memory_type IN (
                       'episodic-leaf', 'episodic-condensed', 'episodic-yearly',
                       'procedural-extract', 'entity-extract', 'theme-consolidation')),
  tier_label         TEXT,                                -- nullable; '' is normalized to NULL
  pass_kind          TEXT NOT NULL CHECK (pass_kind IN
                       ('single', 'verify_fidelity', 'best_of_n_judge')),
  version            INTEGER NOT NULL,
  template           TEXT NOT NULL,
  model_recommendation TEXT,                              -- per-prompt model override
  created_at         TEXT NOT NULL DEFAULT (datetime('now')),
  active             INTEGER NOT NULL DEFAULT 1,
  bundle_version     INTEGER NOT NULL DEFAULT 1,
  notes              TEXT,
  UNIQUE(memory_type, tier_label, pass_kind, version)
);

-- Partial index — at most one active row per triple
CREATE INDEX lcm_prompt_registry_active_idx
  ON lcm_prompt_registry (memory_type, tier_label, pass_kind)
  WHERE active = 1;

-- NULL-safe UNIQUE: SQLite admits multiple NULLs in a UNIQUE column
-- so we COALESCE for the real uniqueness check
CREATE UNIQUE INDEX lcm_prompt_registry_uniq_lookup
  ON lcm_prompt_registry (memory_type, COALESCE(tier_label, ''), pass_kind, version);
```

**Append-only semantics:** `registerPrompt()` opens `BEGIN IMMEDIATE`, computes `max(version)+1` for the triple (counting active + archived rows), flips the previous-active row to `active=0`, inserts the new row with `active=1`, commits. Old versions are never deleted — they stay for traceability + so cache rows pointing at an archived `prompt_id` can still resolve via `getPromptById`.

**Lookup contracts:**

- `getActivePrompt(db, { memoryType, tierLabel, passKind }) → PromptRecord | null` — returns the highest-version active row. Empty-string `tierLabel` is normalized to NULL (matches the COALESCE-based UNIQUE index).
- `getPromptById(db, promptId) → PromptRecord | null` — exact lookup, used by cache reads to resolve the prompt that produced cached text (even if archived).
- `listActivePrompts(db) → PromptRecord[]` — for `/lcm health` operator surface.
- `bumpBundleVersion(db) → number` — atomic `UPDATE active=1 rows SET bundle_version=bundle_version+1`. Triggers voice-consistency rebuild across the synthesis tier when a prompt set is updated as a coordinated unit.

**Seeding** (`seedDefaultPrompts(db)` from `seed-default-prompts.ts`) inserts 10 default prompts from architecture-v4.1.md §12 Appendix A, covering:

1. `episodic-leaf / null / single` — the universal leaf summarizer
2. `episodic-condensed / daily / single`
3. `episodic-condensed / weekly / single`
4. `episodic-condensed / monthly / single`
5. `episodic-condensed / monthly / verify_fidelity`
6. `episodic-yearly / yearly / single` (one of 3 best-of-N candidates)
7. `episodic-yearly / yearly / best_of_n_judge`
8. `episodic-condensed / custom / single` (used by `lcm_synthesize_around`)
9. `episodic-condensed / filtered / single` (grep-filtered path)
10. `procedural-extract / null / single` (strict JSON output)
11. `entity-extract / null / single` (strict JSON array output)

Idempotent: skip-if-any-row-exists for the triple. Operator overrides are NEVER clobbered. Implemented as raw `INSERT` (NOT `registerPrompt`) so it runs INSIDE the outer migration transaction without a nested-BEGIN error.

**Operator override surface:** **No `/lcm prompts add` slash-command exists in the TS codebase as of PR #613.** Updates happen via direct `registerPrompt()` calls (Phase-2 operator-runtime path, see `LCM_PROMPT_*` env hooks in `lcm-command.ts`). The Hermes port may want to expose a `/lcm prompts add|list|show` CLI — see ADR-? below.

## Cache invalidation

When a leaf gets soft-purged (`summaries.suppressed_at = datetime('now')`), the purge path explicitly invalidates dependent caches:

```sql
-- src/operator/purge.ts:346-352
DELETE FROM lcm_synthesis_cache
 WHERE cache_id IN (
   SELECT DISTINCT cache_id FROM lcm_cache_leaf_refs
    WHERE leaf_summary_id IN (?, ?, ...)
 );
```

Why explicit DELETE rather than relying on FK cascade: `lcm_cache_leaf_refs` has `ON DELETE CASCADE` in both directions, but cascade only fires on hard `DELETE summaries`, not on soft suppression (where the row stays put with `suppressed_at` set). The explicit DELETE is necessary to prevent post-suppression cache reads from surfacing PII that was baked into the synthesis before suppression.

The leaf-refs table is populated by `lcm-synthesize-around-tool.ts:1396` right after a successful synthesis:

```sql
INSERT OR IGNORE INTO lcm_cache_leaf_refs (cache_id, leaf_summary_id) VALUES (?, ?);
-- run once per leaf in the source set
```

Best-effort: an insert failure is logged but does not fail the synthesis (the cache row remains; worst case it survives a later suppression and the next operator audit catches it).

Schema:

```sql
CREATE TABLE lcm_cache_leaf_refs (
  cache_id         TEXT NOT NULL REFERENCES lcm_synthesis_cache(cache_id) ON DELETE CASCADE,
  leaf_summary_id  TEXT NOT NULL REFERENCES summaries(summary_id) ON DELETE CASCADE,
  PRIMARY KEY (cache_id, leaf_summary_id)
);
CREATE INDEX lcm_cache_leaf_refs_by_leaf_idx ON lcm_cache_leaf_refs (leaf_summary_id);
```

## Audit

Schema (`lcm_synthesis_audit`):

```sql
CREATE TABLE lcm_synthesis_audit (
  audit_id              TEXT NOT NULL PRIMARY KEY,
  pass_session_id       TEXT NOT NULL,                       -- groups passes of one synthesis attempt
  target_summary_id     TEXT REFERENCES summaries(summary_id) ON DELETE CASCADE,
  target_cache_id       TEXT REFERENCES lcm_synthesis_cache(cache_id) ON DELETE CASCADE,
  prompt_id             TEXT NOT NULL REFERENCES lcm_prompt_registry(prompt_id) ON DELETE RESTRICT,
  pass_kind             TEXT NOT NULL,                       -- 'single' | 'verify_fidelity' | 'best_of_n_judge'
  pass_input_truncated  TEXT NOT NULL,                       -- input bytes, truncated to 8000 chars
  pass_output           TEXT,                                -- nullable until status flips off 'started'
  status                TEXT NOT NULL DEFAULT 'started'
                         CHECK (status IN ('started', 'completed', 'failed')),
  model_used            TEXT NOT NULL,
  latency_ms            INTEGER,
  cost_usd_cents        INTEGER,
  last_error            TEXT,
  ran_at                TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (target_summary_id IS NOT NULL OR target_cache_id IS NOT NULL)
);
```

Indexes: `(target_summary_id, ran_at DESC)`, `(target_cache_id, ran_at DESC)`, `(pass_session_id)`, partial `(ran_at) WHERE status = 'started'` (GC of orphans), partial `(ran_at) WHERE status IN ('completed', 'failed')` (30-day age-out sweep).

**Insert-before-call pattern:** dispatch.ts:402 inserts `status='started'` BEFORE calling the LLM, then updates to `completed`/`failed` after. This guarantees a forensic record exists even if the process crashes between LLM call and ack — operators can later sweep orphan `started` rows older than 1h. The insert is wrapped in try/catch so FK/CHECK violations surface as typed `SynthesisDispatchError("audit_insert_failure")` before any LLM spend.

**pass_session_id semantics:** all passes of a logical synthesis attempt share one `pass_session_id` — for monthly that's `[single, verify_fidelity]`, for yearly that's `[3× single, 1× judge]`. Per-pass disambiguation lives in `pass_input_truncated` (different per candidate) and the sequential `ran_at` timestamps. Wave-7 Auditor #5 P1.1 fix: best-of-N runs candidates via `Promise.allSettled` rather than `Promise.all`, so one failed candidate doesn't discard successful peers; judge picks among survivors.

**`hallucination_flag`** is not a column — it's derived per-call from the `verify_fidelity` output. The contract: `OK\b` at start-of-string OR start-of-line passes; `UNSUPPORTED:` or `HALLUCINATION:` markers anywhere fail. Both conditions checked (Wave-4 P0 fix: a previous relaxation matched `UNSUPPORTED: X\nOK on rest` and CLEARED the flag — flagged monthlies landed in cache as `ready`). The dispatch result surfaces `hallucinationFlagged: boolean | undefined` (undefined when not a monthly tier).

**Truncation:** `pass_input_truncated` and `pass_output` are truncated to 8000 chars with `…(truncated)` marker. Full inputs are not retained.

## Hermes cross-reference: model selection

Hermes already has an LLM client abstraction. The expected wiring point is `agent/llm_client.py` (or whatever the equivalent is named in the Python tree — verify against the Hermes repo at port time). The port should:

1. Define a `LlmCall` protocol in `synthesis/dispatch.py`:

   ```python
   class LlmCall(Protocol):
       async def __call__(
           self, *,
           model: str,
           prompt: str,
           pass_kind: PassKind,
           max_output_tokens: int | None = None,
       ) -> LlmCallResult: ...
   ```

2. Provide an adapter in `synthesis/llm_adapter.py` that wraps Hermes's existing client (likely `anthropic.AsyncAnthropic` or similar) into this protocol. The adapter is the **only** place that knows about Anthropic-vs-OpenAI-vs-other vendors; dispatch stays pure.

3. Map the tier → model decision per the table above. Two implementation options:
   - **Option 1 (matches TS):** all tiers default to `LCM_SUMMARY_MODEL` env var (or `LossLessHermesConfig.summary_model`), tier-specific overrides via per-prompt-row `model_recommendation`.
   - **Option 2 (Hermes-opinionated):** seed the prompts with the haiku/sonnet/opus ladder baked into `model_recommendation`. Operator can still override via per-prompt update.

   The TS source chose Option 1. The port can stay aligned, or switch to Option 2 for a better out-of-box experience. See ADR-? below.

4. Cost accounting: Hermes's client should return prompt/completion token counts. The adapter computes `cost_usd_cents` from the model's per-token rates (Anthropic publishes; bake a table into the adapter). Audit row stores cents as `INTEGER` so the column type matches the TS schema.

## Python class skeleton

```python
# src/lossless_hermes/synthesis/dispatch.py

from typing import Literal, Protocol, TypedDict
from dataclasses import dataclass

TierLabel = Literal["daily", "weekly", "monthly", "yearly", "custom", "filtered"]
PassKind = Literal["single", "verify_fidelity", "best_of_n_judge"]
MemoryType = Literal[
    "episodic-leaf", "episodic-condensed", "episodic-yearly",
    "procedural-extract", "entity-extract", "theme-consolidation",
]

PASS_STRATEGY_BY_TIER: dict[TierLabel, list[PassKind]] = {
    "daily":    ["single"],
    "weekly":   ["single"],
    "monthly":  ["single", "verify_fidelity"],
    "yearly":   ["best_of_n_judge"],
    "custom":   ["single"],
    "filtered": ["single"],
}

HARD_CAP_BEST_OF_N = 5  # Wave-4/5 P1 fix — bound yearly cost

@dataclass(frozen=True)
class LlmCallArgs:
    model: str
    prompt: str
    pass_kind: PassKind
    max_output_tokens: int | None = None

@dataclass(frozen=True)
class LlmCallResult:
    output: str
    latency_ms: float
    cost_cents: int | None = None
    actual_model: str | None = None

class LlmCall(Protocol):
    async def __call__(self, args: LlmCallArgs) -> LlmCallResult: ...

@dataclass
class SynthesizeRequest:
    tier: TierLabel
    memory_type: MemoryType
    source_text: str
    pass_session_id: str
    target_summary_id: str | None = None
    target_cache_id: str | None = None
    model_override: str | None = None
    force_model: bool = False
    best_of_n: int = 3

@dataclass
class SynthesizeResult:
    output: str
    primary_prompt_id: str
    audit_ids: list[str]
    total_latency_ms: float
    total_cost_cents: int
    hallucination_flagged: bool | None = None
    best_of_n: "BestOfNDetail | None" = None

@dataclass
class BestOfNDetail:
    n: int
    selected_index: int
    candidates: list[str]
    requested: int | None = None
    capped: bool = False

class SynthesisDispatchError(Exception):
    def __init__(
        self,
        kind: Literal[
            "missing_prompt", "missing_target", "llm_failure",
            "judge_failure", "audit_insert_failure",
        ],
        message: str,
    ) -> None:
        super().__init__(message)
        self.kind = kind

class SynthesisDispatcher:
    def __init__(self, db: sqlite3.Connection, llm_call: LlmCall) -> None: ...

    async def synthesize(self, req: SynthesizeRequest) -> SynthesizeResult:
        """Main entrypoint. Raises SynthesisDispatchError on missing prompt /
        missing target / llm failure / judge failure / audit insert failure."""
        ...

    # ---- internals (one method per branch) ----
    async def _run_single(self, req, prompt, model) -> _PassResult: ...
    async def _run_verify_fidelity(self, req, candidate_summary, model) -> bool: ...
    async def _run_best_of_n_yearly(self, req, prompt, model, best_of_n: int) -> SynthesizeResult: ...

    # ---- audit ----
    def _insert_audit_started(self, audit_id, ctx, llm_args) -> None: ...
    def _update_audit_completed(self, audit_id, result) -> None: ...
    def _update_audit_failed(self, audit_id, err: str) -> None: ...

    # ---- helpers ----
    def _pick_model(self, req: SynthesizeRequest, prompt: PromptRecord) -> str: ...
    def _render_prompt(self, template: str, req: SynthesizeRequest) -> str: ...
    def _render_verify_prompt(self, template, *, source_text, candidate_summary) -> str: ...
    def _render_judge_prompt(self, template, *, source_text, candidates) -> str: ...
    def _parse_judge_output(self, output: str, n: int) -> int: ...
    def _truncate_for_audit(self, s: str, max_len: int = 8000) -> str: ...

# Free functions in prompt_registry.py
def get_active_prompt(db, *, memory_type, tier_label, pass_kind) -> PromptRecord | None: ...
def get_prompt_by_id(db, prompt_id: str) -> PromptRecord | None: ...
def register_prompt(db, opts: RegisterPromptOptions) -> str: ...
def list_active_prompts(db) -> list[PromptRecord]: ...
def bump_bundle_version(db) -> int: ...

# Free function in seed_prompts.py
def seed_default_prompts(db) -> dict[str, int]:  # {seeded: int, skipped: int}
    """Idempotent; safe inside the migration tx (raw INSERT, no nested BEGIN)."""
    ...
```

## Behavioral parity checklist (port-time tests)

These bugs were caught during the LCM v4.1 review loops. The Python port MUST replicate the fixes — write a regression test for each.

1. **`missing_target` validates BEFORE the LLM call.** Throw `SynthesisDispatchError("missing_target")` if neither `target_summary_id` nor `target_cache_id` is set (Group D adversarial Gap 1).
2. **`force_model` without `model_override` falls back to the tier default** (NOT the prompt's `model_recommendation`). Wave-4 Auditor #5 P1.
3. **Best-of-N hard cap = 5.** Surface `requested` + `capped` fields on result so callers see the clamp. Wave-5 P2.
4. **All best-of-N candidates + judge share one `pass_session_id`.** Don't suffix `_cand{i}`. Group D adversarial Gap 2.
5. **Verify-fidelity regex:** `^OK\b` at start-of-string OR start-of-line passes; `UNSUPPORTED:` / `HALLUCINATION:` at start-of-line fail. Both conditions checked. Wave-4 Auditor #5 P0.
6. **Judge output parser:** prefer `Winner: N`; fall back to "last digit in output, scan backwards"; out-of-range raises `SynthesisDispatchError("judge_failure")` with `N` in the message. Final.review.3 Loop 4 Bug 4.3.
7. **`Promise.allSettled` not `Promise.all` for yearly candidates.** Single-candidate survivor → skip the judge entirely (judge over N=1 is a foot-gun). Populate full SynthesizeResult shape. Wave-7 P1.1/P1.2 + Wave-8 P1 CRITICAL.
8. **Audit insert wrapped in try/catch.** FK/CHECK violations → `SynthesisDispatchError("audit_insert_failure")` BEFORE the LLM is called. Group D adversarial Gap 4.
9. **Verify-prompt placeholder aliases:** both `{{source_text}}`/`{{source_leaves}}` AND `{{candidate_summary}}`/`{{draft}}` substitute. Final.review.3 Loop 4 Bug 4.2.
10. **Cache-lookup UNIQUE index** has all 7 fields including `tier_label` + `prompt_id`. Wave-10 reviewer P1.
11. **Empty-string `tier_label` normalizes to NULL** in both `get_active_prompt` and `register_prompt`. Group D adversarial Gap 3.
12. **Soft-suppress invalidates cache rows via `lcm_cache_leaf_refs` lookup → explicit DELETE** (not relying on FK cascade). Final.review.3 Loop 2 Leak 2.5.
13. **Seeded prompts: NO `{{date_range}}` or `{{target_length}}` placeholders** in templates. Wave-9 Agent #2/#7 P1 (renderer doesn't substitute those, so they'd ship verbatim to the LLM).

## Open decisions

### ADR-031: Tier-to-model mapping policy

**Status:** RESOLVED → [ADR-031](../adr/031-synthesis-tier-model-routing.md) (Accepted 2026-05-14, issue 07-10).

**Question:** Should the Python port replicate the TS "all tiers default to `LCM_SUMMARY_MODEL` env var" policy, or seed an opinionated haiku/sonnet/opus ladder via `model_recommendation`?

**Options:**
- **A.** Match TS exactly (single env var, NULL `model_recommendation` in seed). Pros: maximum simplicity, operator has one knob. Cons: out-of-box behavior is "everything runs on one model" — yearly synthesis wastes compute on haiku, daily wastes money on opus.
- **B.** Seed the ladder. Pros: better out-of-box. Cons: requires operator to learn the override surface to change it; couples the port to Anthropic model names baked in seed text.
- **C.** Hybrid — keep TS's env var as the global default, but seed `model_recommendation` for `yearly + best_of_n_judge` only (where best-of-N is expensive enough that downgrading to a smaller model is a clear win).

**Decision (ADR-031, 2026-05-14):** Option A. The Python port matches TS exactly for v0.1 — single `LCM_SUMMARY_MODEL` env var with `"gpt-5.4-mini"` fallback; all `model_recommendation` rows NULL in `seed_prompts.py`. Per-prompt operator override remains via `register_prompt(model_recommendation=...)`. The opinionated B/C ladder is deferred until Epic 09 eval data exists; the deferral marker is `lossless_hermes.synthesis.tier_routing.TIER_LADDER_DEFERRED`. See ADR-031 for the full rationale + the path to flip to B/C in a future ADR.

### ADR-?: Prompt versioning storage — table vs. git-tracked files

**Question:** Should prompts continue to live in the SQLite `lcm_prompt_registry` table (TS approach), or move to git-tracked YAML/Markdown files with the table holding only operator overrides?

**Options:**
- **A.** Keep table-as-source-of-truth (TS approach). Pros: append-only history is enforced by DB; cache rows FK directly. Cons: prompts are buried in a binary blob; code review of prompt changes is awkward; rollback requires DB write.
- **B.** Git-tracked files as truth, table as cache. Pros: prompts are diffable in PRs; rollback is a `git revert`. Cons: cache-FK pointing at a `prompt_id` needs a stable ID across file edits; bundle_version bump becomes a separate concern.

**Recommendation:** A, for now — the FK from `lcm_synthesis_cache.prompt_id` to `lcm_prompt_registry.prompt_id` is load-bearing for selective cache invalidation. Move to B only if a spike shows the prompt-diff pain is real.

### ADR-?: Audit-row privacy / retention

**Question:** What gets logged into `pass_input_truncated` and `pass_output`? Both currently truncate to 8000 chars but otherwise log full LLM-call input/output, which may contain PII.

**Options:**
- **A.** Match TS (8000-char truncation, no redaction). Pros: simplest. Cons: 8000 chars is plenty to leak names, emails, secrets in plaintext to an operator with DB access.
- **B.** Truncate + redact known patterns (email, SSN, common secret formats) before insert.
- **C.** Make logging opt-in via env var `LCM_AUDIT_LOG_BODIES=0|1` (default 0 in prod, 1 in dev).
- **D.** Hash-only — store SHA-256 of input/output, not the bodies. Cons: can't replay or debug from audit alone.

**Recommendation:** C for the port. Operator can opt into bodies for debugging. Document the retention window (30 days per the GC index) in `/lcm health`.

### ADR-?: Operator override surface

**Question:** Should the port expose `/lcm prompts add|list|show|diff` slash-commands? The TS source has none.

**Options:**
- **A.** Match TS (no slash-commands, registration is API-only). Operators edit via a Python REPL or a one-off script. Pros: zero surface to maintain. Cons: high friction for ops.
- **B.** Add a small CLI shelling out to `register_prompt`/`list_active_prompts`. Pros: low cost. Cons: drift from upstream.

**Recommendation:** B — the Python port's CLI surface is already richer than OpenClaw's; one more command is cheap. Spec out as part of Epic 08-cli-ops, NOT Epic 07.

## Remaining 5% risk

1. **LLM cost-accounting drift.** TS records `cost_usd_cents` as caller-supplied. Hermes-side adapter must compute it from token counts × per-model rates. If rates change (Anthropic price updates), historical audit rows are wrong. Mitigation: store `prompt_tokens` and `completion_tokens` as separate columns in addition to `cost_usd_cents`, recompute on demand.
2. **`Promise.allSettled` semantics in Python.** `asyncio.gather(*, return_exceptions=True)` is the equivalent. Verify the exception-as-value pattern survives the port — especially that `SynthesisDispatchError` from one candidate doesn't poison sibling futures' results.
3. **Token-budget enforcement for yearly best-of-N.** TS has the HARD_CAP_BEST_OF_N=5 clamp but does NOT bound source_text size globally; `lcm_synthesize_around` enforces `MAX_SOURCE_TEXT_TOKENS = 50_000` upstream. Verify the port enforces the same cap or passes it through.
4. **Extended-thinking integration.** If the port seeds `model_recommendation = "claude-opus-4"` with extended thinking for yearly judge, the LlmCall adapter needs to set `thinking={"type": "enabled", "budget_tokens": ...}` on the Anthropic SDK call. The TS source has no thinking-mode support; this is a new surface.
5. **`prompt_id` collisions with the random-suffix generator.** TS uses `Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, '0')` — 24 bits, ~16M space. With ~10 active prompts and infrequent updates, collisions are ~impossible, but the port should still surface the PK violation as a typed error rather than swallowing it.
6. **Cross-database compatibility.** TS uses `node:sqlite` (`DatabaseSync`). Python uses stdlib `sqlite3` OR `apsw` per spike-001. The port must verify that both behave the same on `BEGIN IMMEDIATE` (used by `register_prompt`) and `INSERT OR IGNORE` (used by the cache single-flight) — apsw's autocommit model differs.
7. **`session_key` fallback chain.** TS's `lcm-synthesize-around-tool.ts:799-814` falls back through 4 sources: targetSummary's session_key → input.sessionKey → resolved conversationIds[0]'s session_key → `"agent:main:main"`. Port must replicate exactly to avoid cross-session cache pollution (Wave-7 Auditor #6 P0).
