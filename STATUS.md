# Status

> **Source-of-truth rule:** This file is a *cached projection* of Git state. If `git log` and this file disagree, **Git wins** — fix this file in the first commit of the resumed session.

## Current state

| Field | Value |
|---|---|
| **Current wave** | **v0.2.0 implementation in progress.** Wave 1 ✅ (#147/#148/#149); Wave 2 ✅ (ADR-033 #154, ADR-034 #152, ADR-035 #155). **P0 #156** (7 ported `lcm_*` tools never dispatched) — PR-0/1/2 merged (#158/#159/#161); **7 of 8 tools now dispatch**; the 8th (`lcm_synthesize_around`) is blocked on the compaction P0. **A second P0 — #164 — was found: compaction is non-functional** (`CompactionEngine` never wired into `LCMEngine`; `compress()` silently passes through). Wave 3 = the #164 Epic-04 wrap-up + ADR-032 (#132), now unified — ADR-032 presupposes a working `compress()`. |
| **Current milestone** | **M12 (v0.2.0) in progress.** Wave 1 + Wave 2 merged; #156 tool-dispatch at 7/8. **Two P0s block the v0.2.0 tag: #156 (last tool) and #164 (compaction non-functional).** M5 ("Compaction working") and M8 ("Entity + synthesis green") were marked done in v0.1.0 but are now known/suspected false — see the milestone table. |
| **Last merged PR** | [#161](https://github.com/electricsheephq/lossless-hermes/pull/161) `fix: dispatch adapter for lcm_compact (#156 PR-2)` |
| **Last commit on main** | `dc6372d` |
| **Latest release** | **v0.1.3** — see `gh release list`. v0.2.0 not yet tagged (P0 #156 + Wave 3 outstanding). |
| **Total PRs merged** | Waves 0–6 (all 122 port issues) + the architecture-review series #126/#138–#142/#145 + **v0.2.0 Wave 1 (#147/#148/#149) + Wave 2 (#152/#154/#155)**. |
| **Open PRs** | None. (#156 PR-0/1/2 merged; PR-3 escalated → `lcm_synthesize_around` folded into the #164 compaction work.) |
| **Total tests** | 4200+ passing (`pytest -m 'not live'`); 6 OS×Python matrix cells. |
| **Schema-diff** | CI `--verify-subset` GREEN with 92/92 objects matched. |
| **Open blockers** | None. B-001/B-002 resolved at the v0.1.0 release-gate (operator-gated, accepted). The architecture review's findings are all either fixed (v0.1.1–v0.1.3) or issue-tracked for v0.2.0 — see below. |
| **Architecture review (vs `hermes-lcm`)** | 12 slices + 2 production-scars audits, all 95%-gated. **Fixed (v0.1.x):** #128/#129 (v0.1.1), #144 durability P0 (v0.1.2), #130 (v0.1.3). **Shipped (v0.2.0 W1/W2):** #65 (#147), #131 (#148), #143 (#149), ADR-034 (#152), ADR-033 (#154), ADR-035 (#155), #156 PR-0/1/2 (#158/#159/#161). **Two integration P0s found in the never-run-live port — #156 (tool dispatch unwired) + #164 (compaction unwired).** v0.2.0 remaining: #164 compaction P0 + ADR-032/#132 (unified Wave 3), `lcm_synthesize_around` (blocked on #164), minor #146/#150/#151/#153/#157/#160/#162/#163. |
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
| M5 | Compaction working | ⚠️ **broken — P0 #164** | Epic 04 delivered the algorithm (`compaction.py`, the ~3,892-LOC `CompactionEngine`) + issues 04-01..04-08 + circuit-breaker — but the engine-side wiring was never built: `CompactionEngine` is never instantiated, `_execute_compaction_core` is a `NotImplementedError` stub, and `compress()` (the Hermes-driven path) silently passes messages through unchanged. Same integration-gap class as M7/#156. Fix = the #164 Epic-04 wrap-up, unified with ADR-032. |
| M6 | Embeddings + +52.5pp lift | 🔄 harness done | Wave 6 — 09-08 (#125) shipped the benchmark harness + measured `fts_only` baseline (paraphrastic R@5 0.0%); the live +52.5pp hybrid number is operator-gated on `VOYAGE_API_KEY` (B-001), documented in `docs/benchmarks/voyage-recall-2026-q2.md` |
| M7 | 8 tools wired | ⚠️ dispatch broken — fix in flight (#156) | Wave 5 — Epic 06 delivered the per-tool ports + schemas + token-gate + recursion-guard + SHA-256 verbatim lint, but the **dispatch wiring was never built** — the 7 ported `lcm_*` tools had schemas the model sees with no `TOOL_DISPATCH` entries (P0 #156, found during the #155 review). Being fixed across v0.2.0 PR-0..PR-3: PR-0 = crash-hardening + `lcm_expand` deferral (ADR-037) + #156 regression scaffold; PR-1..PR-3 wire the 6 remaining ported tools via a dispatch-adapter layer. `lcm_status`/`lcm_doctor` (#155) already dispatch. |
| M8 | Entity + synthesis green | ⚠️ suspect — verify under #164 | Epic 07 delivered coreference, extractor, synthesis dispatch/cache_key/invalidation/tier-routing/audit — but synthesis needs an `LlmCall` and `LCMEngine` has no summarizer/`complete` surface (the same gap that breaks compaction, #164). Whether synthesis runs end-to-end is unverified; to be checked as part of the #164 summarizer-surface work. |
| M9 | All operator commands; import-openclaw verified | ✅ done | Wave 5 — Epic 08 (15 issues ported: status/health/purge/backup/reconcile/doctor-{shared,apply,cleaners}/worker-orchestrator/worker-status/rotate/eval-runner/semantic-infra/import-openclaw; 08-11/08-12 superseded by 05-11/07-04) |
| M10 | Eval suite green; drift CI live | ✅ done | Wave 6 — Epic 09: 09-01..09-08 all merged (#115/#120-125, incl. drift CI + live-eval workflow + benchmark harness) |
| M11 | v0.1.0 release | ✅ done | Wave 6 — 12-item release-gate checklist passed (9 PASS; items 6/7/11 operator-gated + documented per B-001/B-002); v0.1.0 tagged on `8b71b12` |
| M12 | v0.2.0 release | 🔄 in progress | Wave 1 ✅ (#147/#148/#149); Wave 2 ✅ (ADR-033/034/035 — #152/#154/#155); #156 tool-dispatch at 7/8 (PR-0/1/2 merged #158/#159/#161). **Two P0s remain before tag: #156 (last tool) + #164 (compaction).** Wave 3 = #164 Epic-04 wrap-up + ADR-032/#132, unified. |

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

_Last refreshed: 2026-05-19 (v0.2.0 Wave 1+2 merged; P0 #156 tool-dispatch at 7/8 — PR-0/1/2 merged #158/#159/#161; second P0 #164 found — compaction non-functional, `CompactionEngine` never wired; M5 downgraded to ⚠️ broken, M8 to ⚠️ suspect; Wave 3 = #164 Epic-04 wrap-up unified with ADR-032/#132. Last commit `dc6372d`.)_
