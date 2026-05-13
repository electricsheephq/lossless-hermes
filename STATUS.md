# Status

> **Source-of-truth rule:** This file is a *cached projection* of Git state. If `git log` and this file disagree, **Git wins** — fix this file in the first commit of the resumed session.

## Current state

| Field | Value |
|---|---|
| **Current wave** | **W1 (Epic 00 Scaffolding) — closing** |
| **Current milestone** | M1 (plugin loads as no-op) — closes after 00-06 lands |
| **Last merged PR** | [#6](https://github.com/electricsheephq/lossless-hermes/pull/6) `[00-08] README + CONTRIBUTING` (merged 2026-05-13T11:09:56Z) |
| **Last commit on main** | `9c2e68c` |
| **Next issue** | [00-06 noop engine](./epics/00-scaffolding/issues/00-06-noop-engine.md) — depends on 00-05 (bridge, merged) + 00-07 (config, merged); ready to dispatch |
| **Open blockers** | None — see [`BLOCKERS.md`](./BLOCKERS.md) |
| **Upstream PR #24949** | filed; LOW-risk additive; awaiting review |
| **Dependabot** | 1 moderate vulnerability flagged; triaged post-Wave-1 |

## Wave 0 — Pre-execution spikes ✅ CLOSED

| ID | Item | Status | Evidence |
|---|---|---|---|
| 0a | Schema-diff CI scaffold | ✅ done | [scripts/schema_diff.sh](./scripts/schema_diff.sh); reference fixture has 92 schema objects from LCM `1f07fbd` |
| 0b | Upstream Hermes preassemble() PR | ✅ filed | [NousResearch/hermes-agent#24949](https://github.com/NousResearch/hermes-agent/pull/24949), 22/22 tests pass |
| 0c | State files bootstrapped | ✅ done | `STATUS.md`, `BLOCKERS.md`, `LEDGER.md`, `docs/upstream/` |
| 0d | GitNexus LCM index verified | ✅ done | `openclaw-code-index` MCP server, repo `lossless-claw`, 7382 nodes, 6836 embeddings |
| 0e | Single-issue dry run on 00-01 | ✅ done | PR #1 merged 2026-05-13; Issue Executor + Pair Reviewer (97% confidence APPROVE) loop validated |

## Wave 1 — Epic 00 Scaffolding (7/8 merged; 00-06 pending)

| Issue | Status | PR |
|---|---|---|
| [00-01 pyproject + package skeleton](./epics/00-scaffolding/issues/00-01-pyproject-and-package-skeleton.md) | ✅ merged | [#1](https://github.com/electricsheephq/lossless-hermes/pull/1) |
| [00-02 CI matrix](./epics/00-scaffolding/issues/00-02-ci-matrix.md) | ✅ merged | [#5](https://github.com/electricsheephq/lossless-hermes/pull/5) |
| [00-03 pre-commit hooks](./epics/00-scaffolding/issues/00-03-precommit-hooks.md) | ✅ merged | [#2](https://github.com/electricsheephq/lossless-hermes/pull/2) |
| [00-04 test harness fixtures](./epics/00-scaffolding/issues/00-04-test-harness-fixtures.md) | ✅ merged | [#4](https://github.com/electricsheephq/lossless-hermes/pull/4) |
| [00-05 hermes bridge stub](./epics/00-scaffolding/issues/00-05-hermes-bridge-stub.md) | ✅ merged | [#3](https://github.com/electricsheephq/lossless-hermes/pull/3) |
| [00-06 noop engine](./epics/00-scaffolding/issues/00-06-noop-engine.md) | ⏳ next | — |
| [00-07 config skeleton](./epics/00-scaffolding/issues/00-07-config-skeleton.md) | ✅ merged | [#7](https://github.com/electricsheephq/lossless-hermes/pull/7) |
| [00-08 README + docs](./epics/00-scaffolding/issues/00-08-readme-and-docs.md) | ✅ merged | [#6](https://github.com/electricsheephq/lossless-hermes/pull/6) |

**Wave 1 exit gate:** 00-06 merged + CI matrix green on `{macOS-latest, ubuntu-latest} × {python-3.11, 3.12, 3.13}` (currently green on main) + Hermes loads plugin as no-op via `hermes` startup log line.

**Wave 1 operational lessons (recorded for Wave 2+):**
1. **Per-worktree isolation is mandatory** for 2+ parallel Executors. Shared working tree caused branch-checkout collisions during Wave 1 (mid-commit branch flips, recovered via post-hoc cherry-picks). Now documented in [`CLAUDE.md`](./CLAUDE.md) as the standing convention.
2. **CI gate-vs-merge timing matters:** PR #7 merged before PR #5's CI workflow landed → format issue slipped through → caught on next PR's rebase. Fix-forward applied. Going forward: dispatch CI workflow PR first when possible.
3. **README rebase pattern:** when one PR rewrites a file and a sibling PR adds to it, `git checkout --theirs` during rebase is the clean resolution (use the rewriter's version).

## Milestone progress

| ID | Milestone | Status | Notes |
|---|---|---|---|
| M0 | Phase 1 doc set complete | ✅ done | Commit 18e9e03 |
| M1 | Plugin loads as no-op | 🟡 in progress | Wave 1 |
| M2 | DB layer feature-complete | ⏳ pending | Wave 2 |
| M3 | Engine round-trips messages | ⏳ pending | Wave 3 |
| M4 | Per-turn ingest + assembly live | ⏳ pending | Wave 4 |
| M5 | Compaction working | ⏳ pending | Wave 5 |
| M6 | Embeddings + +52.5pp lift | ⏳ pending | Wave 5 |
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
