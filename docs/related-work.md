# Related work

> **Historical document (repo archived 2026-07-15).** This positioning analysis predates our decision
> to contribute LCM features directly to [`stephenschoettler/hermes-lcm`](https://github.com/stephenschoettler/hermes-lcm)
> instead of maintaining a parallel plugin. The feature gaps it describes are the ones we are now
> upstreaming there (tracker [hermes-lcm#375](https://github.com/stephenschoettler/hermes-lcm/issues/375)).
> Kept for engineering history; do not read it as current guidance.

> **Purpose:** catalog the related-work landscape around `lossless-hermes` so the v0.1.0 README, ARCHITECTURE.md, and future reviewers see a coherent positioning story. Closes issue [#67](https://github.com/electricsheephq/lossless-hermes/issues/67).

## TL;DR

`lossless-hermes` was a faithful port of the LCM v4.1 TypeScript codebase (`lossless-claw` `pr-613` @ `1f07fbd`) to a Hermes-Agent plugin. The closest neighbor is `stephenschoettler/hermes-lcm`, an independent, shipping Python plugin written from the LCM paper. This port explored **byte-compat OpenClaw migration**, **Voyage embeddings + hybrid search** (which *targeted* the +52.5pp Eva recall lift — a TS-baseline figure never reproduced in this Python port; see [`docs/benchmarks/voyage-recall-2026-q2.md`](./benchmarks/voyage-recall-2026-q2.md)), **Wave-N audit-fix preservation**, and **schema-diff CI**. Those capabilities are now being contributed upstream to `hermes-lcm` rather than shipped here.

---

## Direct neighbors

### `stephenschoettler/hermes-lcm` — Hermes plugin, written from paper

| Dimension | Value |
|---|---|
| **Repo** | [stephenschoettler/hermes-lcm](https://github.com/stephenschoettler/hermes-lcm) |
| **Created** | 2026-04-07 |
| **Status** | shipping `v0.10.4` (2026-05-12) |
| **Adoption** | 493 ★, 33 forks, 5 watchers |
| **Pedigree** | "Inspired by lossless-claw for OpenClaw. Based on the LCM paper by Ehrlich & Blackman (Voltropy PBC, Feb 2026)." — written from paper, not ported from TS |
| **Hermes integration** | Pinned to upstream PR [#7464](https://github.com/NousResearch/hermes-agent/pull/7464) (merged 2026-04-11), which provides the `ContextEngine` ABC + plugin-loader slot |
| **Architecture** | Flat module layout at repo root (no `src/`); single `engine.py` ~3000 LOC; `dag.py` for assembly; one `tools.py` for all tools; sync-only via `threading.Lock` |

#### What it has

- DAG with depth-aware summaries (D0 leaf → D3+ condensed) — semantically equivalent to LCM v4.1's pyramid
- FTS5 retrieval with `recency`/`relevance`/`hybrid` sort modes
- 7 of LCM v4.1's tools: `lcm_grep`, `lcm_load_session`, `lcm_describe`, `lcm_expand`, `lcm_expand_query`, plus `lcm_status` and `lcm_doctor` (the last two are tools-not-slash-commands in their shape — slight divergence)
- **Operator surface ahead of us:** session-glob ignore patterns, message-regex ignore patterns, large-payload externalization with `data:*;base64` detection, profile-scoped DB
- **Hermes plugin manifest** (`plugin.yaml`) — already loaded by upstream Hermes via PR #7464
- **OpenClaw `lcm.db` import path** — opt-in operator script with idempotent `--import-id`, `openclaw-lcm:*` source provenance
- ~800KB of tests across 8 files

#### What it lacks (vs LCM v4.1)

- ❌ Voyage embeddings layer entirely absent (no `voyage/client.py`, no embeddings store)
- ❌ Hybrid/semantic search — they have FTS5 only
- ❌ `sqlite-vec` virtual-table integration
- ❌ Async worker pattern with cross-process `lcm_worker_lock` (TTL + heartbeat + soak)
- ❌ Entity coreference + synthesis profile rebuild
- ❌ Theme consolidation
- ❌ Wave-1..Wave-12 audit fixes from `lossless-claw` (written-from-paper, so the scar tissue wasn't ported)
- ❌ Byte-compat with OpenClaw LCM schema (they import via translation, not shared schema)
- ❌ Schema-diff CI that gates drift from canonical LCM SQLite shape

#### Where they have features we don't yet

| Feature | hermes-lcm file | Our equivalent / status |
|---|---|---|
| Payload guard at ingest boundary | `ingest_protection.py` (22KB) | issue [#61](https://github.com/electricsheephq/lossless-hermes/issues/61) (Wave 5 follow-up) |
| Session glob ignore patterns | `session_patterns.py` | issue [#62](https://github.com/electricsheephq/lossless-hermes/issues/62) (Wave 5, Epic 08) |
| Synthesis tier-to-model routing | `escalation.py` | issue [#63](https://github.com/electricsheephq/lossless-hermes/issues/63) (resolves R21) |
| FTS5 query construction (directness score, age decay, LIKE fallback) | `search_query.py` | issue [#64](https://github.com/electricsheephq/lossless-hermes/issues/64) (Wave 5, Epic 06) |
| Path-security regression tests | `tests/test_path_*.py` | issue [#65](https://github.com/electricsheephq/lossless-hermes/issues/65) (Wave 6) |
| `lcm_load_session` tool | `tools.py` | issue [#66](https://github.com/electricsheephq/lossless-hermes/issues/66) (v0.2) |

#### Architectural disagreements (where we deliberately diverge)

| Choice | hermes-lcm | lossless-hermes | Why |
|---|---|---|---|
| Module layout | flat root | `src/lossless_hermes/` | ADR-024 — preserves importability outside a plugin context, matches modern Python packaging conventions |
| Async/sync | sync-only (`threading.Lock`) | sync hooks (post-PR #34) + async-internal `WorkerLoop` for embeddings backfill | ADR-018 + ADR-020 — Voyage's HTTP retry/backoff state machine doesn't fit clean in sync; we keep the hook surface sync but the worker async |
| Substitution seam | `compress()` (PR #7464 path) | originally `preassemble()` + experimental fallback (ADR-010), to be revised per issue [#60](https://github.com/electricsheephq/lossless-hermes/issues/60) | Our ADR-010 predates PR #7464; investigation at `docs/upstream/001a-preassemble-vs-7464-investigation.md` aligns us with their path |
| Schema-vs-LCM | own schema, import via translation | byte-compat with LCM `1f07fbd`, validated by schema-diff CI | OpenClaw users can `cp ~/.openclaw/lcm.db $HERMES_HOME/lossless-hermes/lcm.db && lossless-hermes import-openclaw` with zero hash drift (per spike 003) |
| Test discipline | integration-heavy (529KB `test_lcm_engine.py`) | unit-test discipline with Wave-N regression fixtures from `lossless-claw` | the `lossless-claw` audit waves produced ~140 scar-tissue tests we explicitly want to preserve |

---

## Upstream foundations

### `martian-engineering/lossless-claw` — the canonical TypeScript LCM

| Dimension | Value |
|---|---|
| **Repo** | [martian-engineering/lossless-claw](https://github.com/martian-engineering/lossless-claw) (canonical) |
| **Pinned commit** | `1f07fbd` on `pr-613` branch (local mirror: `/Volumes/LEXAR/Claude/lossless-claw`) |
| **Status** | active in OpenClaw production |
| **Wave audit history** | 12 audit waves (Wave-1 through Wave-12), ~140 distilled fixes, catalogued in [ADR-029](./adr/029-wave-fix-provenance.md) |
| **Indexed by** | GitNexus `openclaw-code-index` (~7,400 nodes, 6,800 embeddings) |

`lossless-hermes` is a verbatim port. Every load-bearing fix from a numbered Wave (e.g., "Wave-2: `Retry-After > 60s` immediate-throw" in Voyage client) carries an inline `# LCM Wave-N (date): description` provenance comment per ADR-029. The Wave-N audit CI workflow (lands in Wave 5) blocks PRs that touch tagged files without preserving the comment.

### `NousResearch/hermes-agent` — the host

| Dimension | Value |
|---|---|
| **Repo** | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) |
| **Plugin slot** | PR [#7464](https://github.com/NousResearch/hermes-agent/pull/7464) (merged 2026-04-11) — `ContextEngine` ABC + `plugins/context_engine/` directory loader |
| **Substitution seam** | `run_agent.py:7565-7606` preflight calls `compress()` BEFORE the LLM API call |
| **Our additive patches** | docs/upstream/{001..004} — most superseded or low-priority post-#7464 |

PR #7464's existence is the foundation that makes any Hermes-LCM plugin work — both `hermes-lcm` and `lossless-hermes` pin against it.

### Hermes built-in `ContextCompressor`

The default `context_compressor` in Hermes core (also at `agent/context_compressor.py`, modified by PR #7464). Triggers post-response when token budget exceeded, generates a flat summary, replaces older turns. This is the path LCM's plugin replaces. Both `lossless-hermes` and `hermes-lcm` register via `ctx.register_context_engine(...)` to take its slot.

### The LCM paper

> Ehrlich, M. & Blackman, J. (Feb 2026). *Lossless Context Management.* Voltropy PBC. [papers.voltropy.com/LCM](https://papers.voltropy.com/LCM)

Source of the algorithm. Both `hermes-lcm` and `lossless-claw` cite it. Our port comes from `lossless-claw` (the production implementation) rather than directly from the paper, which is how we inherit the 12 waves of audit fixes the paper doesn't describe.

---

## Adjacent (different problem space)

### `ml-explore/mlx-lm` — local inference backends

Hermes supports MLX as a backend; LCM is orthogonal (works regardless of which inference engine is downstream). No direct overlap.

### Anthropic prompt-caching primitives

LCM is **cache-friendly, not fully cache-aware** (per ADR-014 § "Recall-policy injection preserves the prompt cache"). We inject the `LOSSLESS_RECALL_POLICY_PROMPT` into the user message position, never the system prompt, so the system-prompt cache prefix stays identical turn-to-turn. `hermes-lcm` explicitly documents this same boundary in their README. Both projects defer full cache-break/cache-touch tracking pending upstream Hermes signals.

### Built-in Hermes `session_search`

A separate path that searches the host-tracked session DB (Hermes's `state.db`). Returns matches across sessions for the user, not the agent. LCM's `lcm_grep` is **agent-facing** — the agent calls it as a tool to drill into LCM's plugin-local DAG. The two are complementary; READMEs of both `hermes-lcm` and `lossless-hermes` will recommend `session_search` for cross-session host-history queries.

---

## Positioning statement (for v0.1.0 README)

Two-sentence form:

> `lossless-hermes` is the LCM v4.1 algorithm ported verbatim from `lossless-claw` TypeScript, preserving 12 waves of audit fixes, the byte-compat OpenClaw migration path, and the full Voyage embeddings stack that *targets* a +52.5pp recall lift on the Eva benchmark (a TS-baseline figure not yet reproduced in the Python port).
>
> For Lossless Context Management on Hermes, use [`stephenschoettler/hermes-lcm`](https://github.com/stephenschoettler/hermes-lcm) — the shipping, actively-maintained plugin. The capabilities this port explored (OpenClaw migration, embeddings + hybrid search, the v4.1 audit-fix lineage) are being contributed there: [hermes-lcm#375](https://github.com/stephenschoettler/hermes-lcm/issues/375).

Three-sentence form (historical — for "Why not just fork hermes-lcm?" in the planned v0.1.0 FAQ):

> `hermes-lcm` is a strong, independent implementation written from the LCM paper, and it ships today. This port instead started from `lossless-claw`'s TypeScript to (a) inherit the 12-wave audit-fix scar tissue that took LCM 14 weeks to earn in production, (b) preserve byte-compat with OpenClaw's `lcm.db` for lossless migration, and (c) carry the Voyage embeddings + hybrid-search stack. In the end the better answer for the ecosystem was one plugin, not two: those capabilities are being upstreamed into `hermes-lcm` and this repository is archived.

---

## Risk/mitigation derived from this analysis

| Risk | Source | Mitigation |
|---|---|---|
| Community fragments between two LCM plugins | hermes-lcm exists with 493★ already | Position on differentiators (above); cross-link in our README; offer the OpenClaw migration as the canonical migration story |
| Reviewers ask "why didn't you fork?" | natural question for v0.1.0 release | This document + issue [#67](https://github.com/electricsheephq/lossless-hermes/issues/67); FAQ entry in README |
| Their operator features (session patterns, externalization) make our plugin feel less polished | shipping today, polished operator surface | Open issues [#61](https://github.com/electricsheephq/lossless-hermes/issues/61), [#62](https://github.com/electricsheephq/lossless-hermes/issues/62) target these gaps in Wave 5 |
| Our `preassemble` ABC patch (PR #24949) is redundant post-#7464 | discovered via parallel-track investigation 2026-05-14 | Issue [#60](https://github.com/electricsheephq/lossless-hermes/issues/60) tracks ADR-031 supersession + dead-code removal |
| Their tests catch bugs we miss; ours catch bugs they miss | different design philosophies | Cross-pollinate test fixtures (not test structure) — capture specific dict shapes that exposed bugs in either project |
| LCM paper authors release a v2 that diverges from both | Voltropy PBC controls the canonical algorithm | Track Voltropy publications; treat as upstream-watch item if it happens |

---

## Cross-references

- [README.md](../README.md) — link from "Related work" section
- [ARCHITECTURE.md](../ARCHITECTURE.md) — system map; link from intro
- [docs/upstream/001a-preassemble-vs-7464-investigation.md](./upstream/001a-preassemble-vs-7464-investigation.md) — the investigation that surfaced hermes-lcm's architectural choice
- [docs/adr/029-wave-fix-provenance.md](./adr/029-wave-fix-provenance.md) — Wave-N scar tissue invariant
- [docs/adr/030-pr-628-stub-tier-deferred.md](./adr/030-pr-628-stub-tier-deferred.md) — v0.2 deferrals
- Issues [#60](https://github.com/electricsheephq/lossless-hermes/issues/60) through [#69](https://github.com/electricsheephq/lossless-hermes/issues/69) — the hermes-lcm pickup backlog
