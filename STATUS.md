# Status

> **Source-of-truth rule:** This file is a *cached projection* of Git state. If `git log` and this file disagree, **Git wins** — fix this file in the first commit of the resumed session.

## Current state

| Field | Value |
|---|---|
| **Current wave** | **W5 ready to dispatch** — Wave 4 CLOSED (Epic 03 + Epic 05 complete; 25 Wave-4 PRs merged); Wave 5 = Epic 04 compaction + Epic 06 tools + Epic 07 entity/synth + Epic 08 CLI/ops (max fan-out, up to 8 concurrent executors) |
| **Current milestone** | M1 ✅, M2 ✅, M3 ✅, **M4 ✅** (per-turn ingest + assembly live: 03-02 ingest body, 03-04..03-08 assembler chain, 03-09 always-on substitution, 03-10 recall-policy, 05-01..05-11 Voyage + vec0 + worker stack); M5 (compaction) + M6 (embeddings lift) + M7 (7 tools) + M8 (entity/synth) + M9 (operator commands) next |
| **Last merged PR** | [#59](https://github.com/electricsheephq/lossless-hermes/pull/59) `[05-10] embeddings: graceful-degradation contract (4 flags surfaced to callers)` |
| **Last commit on main** | `1d93355` |
| **Total PRs merged today** | **59** (Wave 0–4 complete + chores + fix-forwards + spec docs) |
| **Total tests** | ~2000 passing across 6 OS×Python matrix cells |
| **Schema-diff** | CI `--verify-subset` GREEN with 92/92 objects matched. |
| **Open blockers** | None — see [`BLOCKERS.md`](./BLOCKERS.md) |
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
| M5 | Compaction working | ⏳ pending | Wave 5 — Epic 04 |
| M6 | Embeddings + +52.5pp lift | ⏳ pending | Wave 5 (eval validation; embeddings stack landed in M4) |
| M7 | 7 tools wired | ⏳ pending | Wave 5 |
| M8 | Entity + synthesis green | ⏳ pending | Wave 5 |
| M9 | All operator commands; import-openclaw verified | ⏳ pending | Wave 5 |
| M10 | Eval suite green; drift CI live | ⏳ pending | Wave 6 |
| M11 | v0.1.0 release | ⏳ pending | Wave 6 |
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

_Last refreshed: 2026-05-13 (after Wave 0 close + PR #1 merge)_
