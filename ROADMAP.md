# Roadmap: lossless-hermes

> **Audience:** anyone scheduling Phase-2 execution.
> **Decisions reference:** [`docs/adr/`](./docs/adr/). **Risk register:** [`docs/risks.md`](./docs/risks.md).

## Headline

**3–5 months single-engineer.** 10 epics, 122 issues, ~38,000 Python LOC. Critical path is **Epic 00 → 01 → 02 → 03 → 04**. After Epic 02 lands, Epics 05, 06, 08 can run in parallel. Epic 09 is terminal. Total effort: **600–900 hours** at 90%+ confidence per the [feasibility analysis](./ARCHITECTURE.md#total-scope) and the [10 porting guides](./docs/porting-guides/).

## Milestones

| Milestone | Description | Trigger | Epic gate |
|---|---|---|---|
| **M0** | Phase 1 doc set complete | All ADRs + spikes + porting guides written, repo seeded | (this set) |
| **M1** | Plugin loads in Hermes as no-op | Epic 00 closed | 00 |
| **M2** | DB layer feature-complete | Epic 01 closed; 200+ pytest cases green | 01 |
| **M3** | Engine wired; round-trip messages through `compress()` | Epic 02 closed | 02 |
| **M4** | Per-turn ingest + assembly running on live conversations | Epic 03 closed | 03 |
| **M5** | Compaction actually compresses | Epic 04 closed; `/lcm health` shows working pyramid | 04 |
| **M6** | Embeddings + hybrid retrieval live; +52.5pp lift reproducible | Epic 05 closed | 05 |
| **M7** | All 7 tools (v0.1.0 set) wired and tested | Epic 06 closed | 06 |
| **M8** | Entity + synthesis pipelines green | Epic 07 closed | 07 |
| **M9** | All operator commands working; `import-openclaw` migration verified | Epic 08 closed | 08 |
| **M10** | Eval suite green; drift CI gating in place | Epic 09 closed | 09 |
| **M11** | v0.1.0 release | All epics closed + integration soak | release |
| **M12** | v0.2.0 release (#628 stub-tier) | Per [ADR-030](./docs/adr/030-pr-628-stub-tier-deferred.md) | follow-up |

## Critical path

```
00 ──► 01 ──► 02 ──► 03 ──► 04 ──► 09 (release-gate eval)
                       │              ▲
                       ├──► 05 ───────┤
                       ├──► 06 ───────┤
                       ├──► 07 ───────┤
                       └──► 08 ───────┘
```

The critical path is 00 → 01 → 02 → 03 → 04. After Epic 02 closes, Epics 05/06/07/08 are parallelizable. Epic 09 is a final integration gate.

## Time-and-effort table

| Epic | Effort (hrs) | Calendar (1 eng) | Critical path | Parallelizable after | Confidence |
|---|---:|---|---|---|---:|
| [00 Scaffolding](./epics/00-scaffolding/README.md) | 30–40 | 1 week | YES | — | 95% |
| [01 Storage](./epics/01-storage/README.md) | 120–180 | 3–4 weeks | YES | Epic 00 | 95% |
| [02 Engine skeleton](./epics/02-engine-skeleton/README.md) | 60–80 | 2 weeks | YES | Epic 01 | 90% |
| [03 Ingest + assembly](./epics/03-ingest-assembly/README.md) | 80–100 | 3 weeks | YES | Epic 02 | 85% |
| [04 Compaction](./epics/04-compaction/README.md) | 60–90 | 3 weeks | YES | Epic 02, 03 | 90% |
| [05 Embeddings](./epics/05-embeddings/README.md) | 40–50 | 2 weeks | no | Epic 02 | 95% |
| [06 Tools](./epics/06-tools/README.md) | 80–100 | 3–4 weeks | no | Epic 02 | 90% |
| [07 Entity + synthesis](./epics/07-entity-synthesis/README.md) | 50–72 | 3 weeks | no | Epic 04, 05 | 85% |
| [08 CLI + ops + doctor](./epics/08-cli-ops/README.md) | 70–90 | 3–4 weeks | no | Epic 02, 04, 05 | 90% |
| [09 Eval + benchmarks](./epics/09-eval/README.md) | 40–50 | 2 weeks | YES (gate) | Epic 05, 06, 07 | 90% |
| **TOTAL (one engineer, serial)** | **630–852** | **24–34 weeks** | | | |
| **TOTAL (one engineer, parallel after Epic 02)** | **630–852** | **17–22 weeks** | | | |

## Calendar (single engineer, 1 full-time)

Assumes 30 hrs/week of focused porting (rest = review, eval, ops, integration).

| Week | Critical | Parallel | Milestone |
|---|---|---|---|
| 1 | Epic 00 | | M1 |
| 2-5 | Epic 01 | | M2 |
| 6-7 | Epic 02 | | M3 |
| 8-10 | Epic 03 | Epic 05 starts | M4 |
| 11-13 | Epic 04 | Epic 05, Epic 06 | M5, M6 |
| 14-16 | (slack/buffer) | Epic 06, Epic 08 | M7 |
| 17-19 | | Epic 07, Epic 08 | M8, M9 |
| 20-22 | Epic 09 | Integration soak | M10 |
| 23-24 | Release prep | | M11 |

## Calendar (two engineers post-Epic 02)

Save ~6 weeks by parallelizing after M3.

| Week | Eng A (critical path) | Eng B (parallel) |
|---|---|---|
| 1 | Epic 00 | (pair on Epic 00) |
| 2-5 | Epic 01 | Epic 01 |
| 6-7 | Epic 02 | (pair on Epic 02) |
| 8-10 | Epic 03 | Epic 05 |
| 11-13 | Epic 04 | Epic 06 (Wave A) |
| 14-16 | Epic 06 (Wave B integration) | Epic 07 + Epic 08 (operator) |
| 17 | Epic 09 | Epic 08 (doctor) |
| 18 | Integration soak | Integration soak |

## Dependency graph

```
[Epic 00 Scaffolding]
        │
        ▼
[Epic 01 Storage]  ───────────► [Epic 05 Embeddings]
        │                              │
        ▼                              │
[Epic 02 Engine skel]                  │
        │                              │
        ├──► [Epic 03 Ingest/Assemble] │
        │           │                  │
        │           ▼                  │
        │      [Epic 04 Compaction] ◄──┘
        │           │
        ├──► [Epic 06 Tools] ───────┐
        │           │                │
        │           ▼                │
        ├──► [Epic 07 Entity/Synth]  │
        │           │                │
        ├──► [Epic 08 CLI/Ops]       │
        │           │                │
        ▼           ▼                ▼
              [Epic 09 Eval / Release gate]
```

## Issue-level porting order (within each epic)

Inside each epic, issues are numbered to suggest a port order — but most pair well. Recommended *single-engineer* daily/weekly order, picking off dependencies as they unblock:

**Week 1 (Epic 00):**
00-01 (pyproject) → 00-04 (test harness) → 00-05 (hermes bridge stub) → 00-06 (no-op engine) → 00-02 (CI) → 00-03 (pre-commit) → 00-07 (config skeleton) → 00-08 (readme).

**Weeks 2–5 (Epic 01):** start with 01-01..03 (connection, config, features); then 01-04..06 (migrations); then 01-07 (identity hash, with the spike-003 fixture); then 01-08..09 (the big stores); then 01-10..15 (telemetry, FTS5 helpers, large-files, integrity, transcript-repair, versioned backfills).

**Weeks 6–7 (Epic 02):** 02-01 (init) → 02-02 (state) → 02-03 (lifecycle) → 02-04 (token tracking) → 02-05 (should_compress) → 02-06 (no-op compress) → 02-07 (hook registrations) → 02-08 (per-session locks) → 02-09 (circuit breaker scaffold) → 02-10 (slash dispatcher).

**Weeks 8–10 (Epic 03):** 03-01 (token estimator) → 03-02 (ingest diff) → 03-03 (handle_tool_call belt-and-suspenders) → 03-04..08 (assembler subsystems) → 03-09 (always-on substitution — pending [ADR-010](./docs/adr/010-always-on-assembly.md) upstream PR) → 03-10 (recall-policy injection).

**Weeks 11–13 (Epic 04):** 04-01 (evaluate) → 04-02 (leaf pass) → 04-03 (condensation) → 04-04 (anti-thrashing) → 04-05 (prompts) → 04-06 (fallback chain) → 04-07 (circuit breaker integration) → 04-08 (telemetry).

**Weeks 11–13 PARALLEL Epic 05:** 05-01 (Voyage client) → 05-02 (credentials) → 05-03 (vec0 store) → 05-04 (load pattern) → 05-05 (worker loop) → 05-06 (worker lock) → 05-07 (backfill) → 05-08 (semantic search) → 05-09 (hybrid search) → 05-10 (degraded modes) → 05-11 (autostart wiring).

**Weeks 14–16 PARALLEL Epic 06:** 06-01..04 (translations + dispatch + middleware + common) → 06-05..06 (scope + recursion guard) → 06-07 (lcm_describe) → 06-08 (lcm_grep Wave A) → 06-10..11 (entity tools) → 06-12 (lcm_expand primitive) → 06-14 (lcm_compact) → 06-13 (lcm_synthesize_around) → 06-09 (lcm_grep Wave B, after Epic 05) → 06-15 (CI lint).

**Weeks 14–17 PARALLEL Epic 07:** 07-01 (CTE) → 07-04 (extraction autostart) → 07-02 (coref worker) → 07-03 (extractor LLM) → 07-08 (prompts) → 07-05 (synthesis dispatch) → 07-06 (cache key) → 07-09 (audit) → 07-07 (invalidation) → 07-10 (tier routing).

**Weeks 15–18 PARALLEL Epic 08:** 08-01 (router) → 08-02..03 (status, health) → 08-09 (backup) → 08-14 (semantic-infra-init) → 08-10..12 (worker, autostart) → 08-04 (purge cascade) → 08-05 (reconcile) → 08-06..08 (doctor) → 08-15 (import-openclaw) → 08-13 (eval-runner) → 08-16..17 (rotate, worker status).

**Weeks 20–22 Epic 09:** 09-01 (query set) → 09-02 (recall) → 09-03 (judge) → 09-04 (run) → 09-05 (fixtures) → 09-06 (drift) → 09-07 (CI live-eval) → 09-08 (benchmarks).

## Decision gates per milestone

Before each milestone closes, verify:

- [ ] All issues in the epic's `epics/<n>-<name>/issues/` are checked off
- [ ] CI green on `{macOS, ubuntu} × {3.11, 3.12, 3.13}`
- [ ] No new TODO/FIXME without an open issue link
- [ ] Wave-N provenance comments preserved per [ADR-029](./docs/adr/029-wave-fix-provenance.md)
- [ ] Test coverage targets met (80% line, mirror LCM's coverage shape)

## Pre-execution gating (M0 → Epic 00)

Before starting Epic 00, verify:

1. [ ] [ADR-010](./docs/adr/010-always-on-assembly.md) upstream Hermes `preassemble()` ABC PR submitted (or accept experimental `should_compress=True` fallback)
2. [ ] Hermes-agent install + plugin-loading manually verified once (`hermes` starts with placeholder plugin)
3. [ ] Voyage API key tested live (per [spike 004](./docs/spike-results/004-voyage-python-client.md) already done)
4. [ ] `sqlite-vec` + FTS5+trigram tested on Homebrew Python and `ubuntu-latest` (spike 001/005 confirmed macOS; Linux CI verifies)
5. [ ] Owner is decided for the upstream PR contributions per [ADR-015](./docs/adr/015-hermes-upstream-patches.md)

## Release readiness for v0.1.0

| Criterion | Source of truth |
|---|---|
| All 9 epic READMEs marked CLOSED | `epics/*/README.md` |
| 122 issue specs ported and merged | `epics/*/issues/*.md` |
| Eval recall+drift reproduces +52.5pp lift | [Epic 09](./epics/09-eval/README.md) |
| Migration from existing OpenClaw `lcm.db` verified end-to-end | [ADR-025](./docs/adr/025-openclaw-migration.md) |
| README install path tested on macOS arm64 + Linux x86_64 | M0 |
| All 30 ADRs status=Accepted (or explicitly Superseded with link) | `docs/adr/` |
| No open BLOCKER risks | [`docs/risks.md`](./docs/risks.md) |

## What's NOT in scope for v0.1.0

Per ADRs:

- **`lcm_expand_query` tool** — deferred to v2 ([ADR-012](./docs/adr/012-subagent-defer.md))
- **#628 stub-tier substitution** — deferred to v0.2.0 ([ADR-030](./docs/adr/030-pr-628-stub-tier-deferred.md))
- **`prepareSubagentSpawn` / `subagentEnded` lifecycle** — deferred ([ADR-012](./docs/adr/012-subagent-defer.md))
- **Transcript-GC (`rewriteTranscriptEntries`)** — dropped (Hermes has no JSONL to rewrite)
- **Auto-rotate session files** — dropped (Hermes uses SQLite session.db, not JSONL)
- **Themes / procedures / intentions / purge_rebuild_queue tables** — removed in LCM's first-principles pass; not ported

## v0.2.0 scope (next release after v0.1.0)

- #628 stub-tier substitution (per [ADR-030](./docs/adr/030-pr-628-stub-tier-deferred.md))
- Voyage Codex-OAuth-profile accordion cadence (small preset; see initial analysis in prior conversation history)
- (Optional) `lcm_expand_query` re-implementation against Hermes `delegate_task`

## How to use this roadmap

- **For execution agents:** start by reading [`README.md`](./README.md), [`ARCHITECTURE.md`](./ARCHITECTURE.md), this file, [`docs/risks.md`](./docs/risks.md). Then open the first issue under [`epics/00-scaffolding/issues/00-01-pyproject-and-package-skeleton.md`](./epics/00-scaffolding/issues/00-01-pyproject-and-package-skeleton.md). Follow the "Files to read before starting" section at the top of each issue.
- **For tracking:** keep this file's milestones table updated. Add `**Status:** ✅` next to each milestone as it closes.
- **For replanning:** if scope changes, update individual epic READMEs first, then propagate to the calendar tables here.
