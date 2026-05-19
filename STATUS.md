# Status

> **Source-of-truth rule:** This file is a *cached projection* of Git state. If `git log` and this file disagree, **Git wins** — fix this file in the first commit of the resumed session.

## Current state

| Field | Value |
|---|---|
| **Current wave** | **W6 — v0.1.0 release gate** — ALL 122 port issues merged (Epics 00–09 complete; 09-08 terminal issue landed #125). Running the 12-item release-gate verification checklist; then v0.1.0 tag. |
| **Current milestone** | M1–M5 ✅; M7/M8/M9 ✅; **M10 ✅** (eval suite — Epic 09 09-01..09-08 all merged, incl. drift CI + live-eval workflow + benchmark harness); M6 — `fts_only` baseline measured offline, live +52.5pp hybrid number operator-gated (B-001); **M11 (v0.1.0) in progress** — release-gate checklist running |
| **Last merged PR** | [#125](https://github.com/electricsheephq/lossless-hermes/pull/125) `[09-08] eval: Voyage recall benchmark + v41-test-corpus Python port` |
| **Last commit on main** | `66c712e` |
| **Total PRs merged** | **109** (Waves 0–6 complete: all 122 port issues + chores + fix-forwards) |
| **Open PRs** | None |
| **Total tests** | ~4050 passing across 6 OS×Python matrix cells |
| **Schema-diff** | CI `--verify-subset` GREEN with 92/92 objects matched. |
| **Open blockers** | **B-001 open** — live +52.5pp Voyage benchmark requires an unprovisioned `VOYAGE_API_KEY`; harness + measured `fts_only` baseline shipped (#125), live hybrid run operator-gated. Recommended for acceptance as a documented v0.1.0 release-gate item. See [`BLOCKERS.md`](./BLOCKERS.md). |
| **Upstream PR #24949** | filed; LOW-risk additive; awaiting review |
| **Dependabot** | ✅ alert #1 (pytest tmpdir CVE) closed by [PR #9](https://github.com/electricsheephq/lossless-hermes/pull/9) |

## Wave 0 — Pre-execution spikes ✅ CLOSED

| ID | Item | Status | Evidence |
|---|---|---|---|
| 0a | Schema-diff CI scaffold | ✅ done | [scripts/schema_diff.sh](./scripts/schema_diff.sh); reference fixture has 92 schema objects from LCM `1f07fbd` |
| 0b | Upstream Hermes preassemble() PR | ✅ filed | [NousResearch/hermes-agent#24949](https://github.com/NousResearch/hermes-agent/pull/24949), 22/22 tests pass |
| 0c | State files bootstrapped | ✅ done | `STATUS.md`, `BLOCKERS.md`, `LEDGER.md`, `docs/upstream/` |
| 0d | GitNexus LCM index verified | ✅ done | `openclaw-code-index` MCP server, repo `lossless-claw`, 7382 nodes, 6836 embeddings |
| 0e | Single-issue dry run on 00-01 | ✅ done | PR #1 merged 2026-05-13; Issue Executor + Pair Reviewer (97% confidence APPROVE) loop validated |

## Wave 1 — Epic 00 Scaffolding ✅ CLOSED

| Issue | Status | PR |
|---|---|---|
| [00-01 pyproject + package skeleton](./epics/00-scaffolding/issues/00-01-pyproject-and-package-skeleton.md) | ✅ merged | [#1](https://github.com/electricsheephq/lossless-hermes/pull/1) |
| [00-02 CI matrix](./epics/00-scaffolding/issues/00-02-ci-matrix.md) | ✅ merged | [#5](https://github.com/electricsheephq/lossless-hermes/pull/5) |
| [00-03 pre-commit hooks](./epics/00-scaffolding/issues/00-03-precommit-hooks.md) | ✅ merged | [#2](https://github.com/electricsheephq/lossless-hermes/pull/2) |
| [00-04 test harness fixtures](./epics/00-scaffolding/issues/00-04-test-harness-fixtures.md) | ✅ merged | [#4](https://github.com/electricsheephq/lossless-hermes/pull/4) |
| [00-05 hermes bridge stub](./epics/00-scaffolding/issues/00-05-hermes-bridge-stub.md) | ✅ merged | [#3](https://github.com/electricsheephq/lossless-hermes/pull/3) |
| [00-06 noop engine](./epics/00-scaffolding/issues/00-06-noop-engine.md) | ✅ merged | [#8](https://github.com/electricsheephq/lossless-hermes/pull/8) |
| [00-07 config skeleton](./epics/00-scaffolding/issues/00-07-config-skeleton.md) | ✅ merged | [#7](https://github.com/electricsheephq/lossless-hermes/pull/7) |
| [00-08 README + docs](./epics/00-scaffolding/issues/00-08-readme-and-docs.md) | ✅ merged | [#6](https://github.com/electricsheephq/lossless-hermes/pull/6) |

**Wave 1 exit gate:** ✅ ALL CRITERIA MET
- ✅ All 8 issues merged
- ✅ CI matrix green on `{macOS-latest, ubuntu-latest} × {python-3.11, 3.12, 3.13}` (latest main run all 6 cells SUCCESS)
- ✅ Plugin registers as no-op via `register(ctx)` → `ctx.register_context_engine(LCMEngine())`
- ✅ Dependabot alert #1 closed

**Plus 2 chore PRs:**
- [chore: ruff format tests/test_config_load.py](https://github.com/electricsheephq/lossless-hermes/commit/b81c7e7) (fix-forward for pre-CI format slip)
- [#9 chore(deps): pytest 9.0.2→9.0.3](https://github.com/electricsheephq/lossless-hermes/pull/9) (dependabot moderate)

**Wave 1 cycle time:** PR #1 merged 10:38Z → PR #9 merged 11:32Z = **~54 minutes** for 9 PRs end-to-end (vs ROADMAP estimate of 1 week).

**Wave 1 operational lessons (recorded for Wave 2+):**
1. **Per-worktree isolation is mandatory** for 2+ parallel Executors. Shared working tree caused branch-checkout collisions during Wave 1 (mid-commit branch flips, recovered via post-hoc cherry-picks). Now documented in [`CLAUDE.md`](./CLAUDE.md) as the standing convention.
2. **CI gate-vs-merge timing matters:** PR #7 merged before PR #5's CI workflow landed → format issue slipped through → caught on next PR's rebase. Fix-forward applied. Going forward: dispatch CI workflow PR first when possible.
3. **README rebase pattern:** when one PR rewrites a file and a sibling PR adds to it, `git checkout --theirs` during rebase is the clean resolution (use the rewriter's version).

## Milestone progress

| ID | Milestone | Status | Notes |
|---|---|---|---|
| M0 | Phase 1 doc set complete | ✅ done | Commit 18e9e03 |
| M1 | Plugin loads as no-op | ✅ done | Wave 1 closed 2026-05-13T11:32Z; 9 PRs in 54 min |
| M2 | DB layer feature-complete | ✅ done | Wave 2 closed 2026-05-13T13:43Z; 13 PRs + 1 chore; 907 tests; schema-diff subset 92/92 |
| M3 | Engine round-trips messages | ✅ done | Wave 3 closed 2026-05-13; 9 PRs covering 10 issues + 2 fix-forwards; 1101 tests; LCMEngine wired through Hermes ABC + 4 plugin hooks + /lcm slash command |
| M4 | Per-turn ingest + assembly live | ✅ done | Wave 4 closed 2026-05-13/14; 25 PRs covering Epic 03 (10 issues: 03-01..03-10) + Epic 05 (11 issues: 05-01..05-11); ContextAssembler.assemble() with full 16-step pipeline; preassemble ABC override (Option B) + experimental force-compress (Option A) coexistent per ADR-010; Voyage HTTP client + sqlite-vec store + WorkerLoop + WorkerLock + backfill cron + semantic + hybrid + degraded-modes contract + autostart |
| M5 | Compaction working | ✅ done | Wave 5 — Epic 04 (04-01..04-08 + circuit-breaker; compaction.py ~3000 LOC) |
| M6 | Embeddings + +52.5pp lift | 🔄 harness done | Wave 6 — 09-08 (#125) shipped the benchmark harness + measured `fts_only` baseline (paraphrastic R@5 0.0%); the live +52.5pp hybrid number is operator-gated on `VOYAGE_API_KEY` (B-001), documented in `docs/benchmarks/voyage-recall-2026-q2.md` |
| M7 | 8 tools wired | ✅ done | Wave 5 — Epic 06 (all per-tool ports + dispatch + token-gate + recursion-guard + SHA-256 verbatim lint) |
| M8 | Entity + synthesis green | ✅ done | Wave 5 — Epic 07 (coreference, extractor, synthesis dispatch/cache_key/invalidation/tier-routing/audit) |
| M9 | All operator commands; import-openclaw verified | ✅ done | Wave 5 — Epic 08 (15 issues ported: status/health/purge/backup/reconcile/doctor-{shared,apply,cleaners}/worker-orchestrator/worker-status/rotate/eval-runner/semantic-infra/import-openclaw; 08-11/08-12 superseded by 05-11/07-04) |
| M10 | Eval suite green; drift CI live | ✅ done | Wave 6 — Epic 09: 09-01..09-08 all merged (#115/#120-125, incl. drift CI + live-eval workflow + benchmark harness) |
| M11 | v0.1.0 release | 🔄 in progress | Wave 6 — all 122 issues merged; running the 12-item release-gate verification checklist |
| M12 | v0.2.0 release (#628 stub-tier) | future | — |

## Upstream watch

See [`docs/upstream/`](./docs/upstream/) for full per-patch status. Quick summary:

| Patch | ADR | Status | PR URL |
|---|---|---|---|
| 001 preassemble ABC | ADR-010 | **filed** | [#24949](https://github.com/NousResearch/hermes-agent/pull/24949) |
| 002 register_command forwarding | ADR-015 #2 | drafted | — |
| 003 engine.ingest hook | ADR-015 #3 | drafted | — |
| 004 cache-token forwarding | ADR-015 #4 | drafted | — |

## Resume checklist (every fresh session, in order)

1. Read this file (STATUS.md) — current wave, milestone, next issue
2. `git log --oneline -20` and `gh pr list --state all --limit 20 --repo electricsheephq/lossless-hermes` — verify Git matches this file
3. Read [`BLOCKERS.md`](./BLOCKERS.md) — anything waiting on Claude
4. Read last 2 rows of [`LEDGER.md`](./LEDGER.md) — cost trajectory
5. Read the "next issue" file(s) linked above — full spec
6. (Wave 3+) Read [`docs/upstream/`](./docs/upstream/) — upstream PR status

**Drift detection:** before claiming "we're at issue X", cross-check this file against `git log --oneline --grep='\[N-MM\]' -1`. If they disagree, Git wins.

---

_Last refreshed: 2026-05-19 (ALL 122 issues merged — Epics 00–09 complete, 109 PRs, main CI green on `66c712e`; in v0.1.0 release-gate verification)_
