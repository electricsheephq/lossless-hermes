# CLAUDE.md — Project-Level Instructions for Agent Sessions

> **Audience:** any Claude Code session working on `electricsheephq/lossless-hermes`.
> **Cardinal rule:** the source of truth for "where are we" is `git log` + `gh pr list`, NOT chat history.

## Resume protocol (every fresh session, in order — under 5 minutes)

1. **Read [`STATUS.md`](./STATUS.md)** — current wave, milestone, next issue
2. **Cross-check Git:** `git log --oneline -20` and `gh pr list --state all --limit 20 --repo electricsheephq/lossless-hermes` — if disagrees with STATUS.md, **Git wins**; fix STATUS.md in the first commit of the resumed session
3. **Read [`BLOCKERS.md`](./BLOCKERS.md)** — anything waiting on Claude decision
4. **Read last 2 rows of [`LEDGER.md`](./LEDGER.md)** — cost trajectory + throttle signal
5. **Read the "next issue" file linked from STATUS** — full spec
6. **(Wave 3+) Read [`docs/upstream/`](./docs/upstream/)** — upstream Hermes PR status

## Project layout (where things live)

- `STATUS.md`, `BLOCKERS.md`, `LEDGER.md` — operational state (repo root)
- [`ROADMAP.md`](./ROADMAP.md) — 7-wave plan with milestones M0–M12
- [`ARCHITECTURE.md`](./ARCHITECTURE.md) — system map + Python filesystem layout
- [`docs/adr/`](./docs/adr/) — 30 Architecture Decision Records (canonical; if anything conflicts with an ADR, the ADR wins)
- [`docs/spike-results/`](./docs/spike-results/) — 5 de-risking spike outcomes
- [`docs/porting-guides/`](./docs/porting-guides/) — 10 per-subsystem TS→Python guides
- [`docs/reference/`](./docs/reference/) — hooks reference, source map, dependencies
- [`docs/upstream/`](./docs/upstream/) — Hermes upstream PR tracking
- [`epics/`](./epics/) — 10 epic READMEs + 122 issue specs (THE work queue)
- `scripts/schema_diff.sh` — load-bearing CI gate; do not break

## Source references (read-only, NOT in this repo)

- **TS source pinned to commit `1f07fbd`**: `/Volumes/LEXAR/Claude/lossless-claw` on branch `pr-613`
- **Hermes host**: `/Volumes/LEXAR/Claude/hermes-agent`
- **GitNexus code-graph queries**:
  - For Hermes: `mcp__hermes-code-index__*` with `repo: hermes-agent`
  - For LCM: `mcp__openclaw-code-index__*` with `repo: lossless-claw` (note: indexed `100yenadmin/lossless-claw@fcd013a9`, close-but-different fork — cross-check exact line refs against `/Volumes/LEXAR/Claude/lossless-claw`)

## Agent orchestration patterns

### Issue Executor + Pair Reviewer (the default loop)

Every port issue ships via this two-agent loop:
1. **Issue Executor agent** receives one issue spec → produces a PR with code + tests + commits
2. **Pair Reviewer agent** (fresh context, no Executor history) → independent adversarial review at 95% confidence → APPROVE or REQUEST_CHANGES
3. **Merge** when CI green + Pair Reviewer approves + acceptance criteria all met

### Per-worktree isolation for parallel dispatches (load-bearing)

**When dispatching 2+ Issue Executors in parallel**, each MUST work in its own `git worktree` to avoid branch-checkout collisions. The shared working tree pattern (Wave 1's initial mistake) caused multiple agents' branch state to flip mid-commit; recovery was via post-hoc `git cherry-pick` and forced isolated worktrees.

**Convention:**
```bash
# Issue Executor session for issue 00-NN:
cd /Volumes/LEXAR/Claude/lossless-hermes
git worktree add ../lossless-hermes-00-NN port/00-NN-name
cd ../lossless-hermes-00-NN
# ...do all work here...
# After PR opens, cleanup:
cd /Volumes/LEXAR/Claude/lossless-hermes
git worktree remove ../lossless-hermes-00-NN --force
```

Pair Reviewer sessions follow the same pattern with their own `-review-N` worktree.

### Quality gates (automatic, blocks merge)

- CI green on `{macOS-latest, ubuntu-latest} × {python-3.11, 3.12, 3.13}`
- `pytest -m 'not live'` passes
- `ty check src` (or expanded scope as Epic 00 issues land)
- `ruff format --check src tests scripts` + `ruff check`
- Pre-commit hooks pass (now-installed)
- Schema-diff zero on any PR touching `src/lossless_hermes/db/migration*`
- Wave-N provenance comment present on tagged files (per [ADR-029](./docs/adr/029-wave-fix-provenance.md))

### Failure recovery patterns

| Failure mode | Response |
|---|---|
| CI fails on PR | Re-prompt **same agent context** with logs; cap 2 retries; then escalate (likely spec ambiguity) |
| Agent compacted mid-issue | Dispatch fresh agent with `git log --oneline` of branch as context; partial uncommitted work lost |
| Pair Reviewer rejects (MAJOR/CRITICAL) | Send back to Executor with review attached; cap 2 cycles before escalation |
| Pair Reviewer NITs only | Batch-fix in follow-up PR; don't block merge |
| Merge conflict on rebase | Resolve with `--theirs` (incoming) or `--ours` (target) depending on which side has authoritative version; document choice in commit message |
| Schema-diff fails | BLOCK all Wave 2 PRs until resolved; data corruption risk |

## Test policy

- **Default verification path is GitHub CI**, not local execution (user-global guidance: main disk is full)
- **LEXAR repos** (this repo + `lossless-claw` + `hermes-agent` + `hermes-agent-fork`) — local commands OK, deps install OK
- **`pytest -m 'not live'`** is the default; live tests gated on `VOYAGE_API_KEY` + `ANTHROPIC_API_KEY` env vars
- Pre-commit hooks installed (`ruff`, `ty`, file-hygiene) — must pass; never `--no-verify`

## Wave-N provenance comments (load-bearing)

Per [ADR-029](./docs/adr/029-wave-fix-provenance.md): scar-tissue fixes from LCM's 12 audit waves MUST port verbatim with `# LCM Wave-N (date): description` comments. Known tagged sites are listed in ADR-029. CI (when wave-n-audit workflow lands) blocks PRs that touch tagged files without the provenance comment.

## Upstream Hermes contributions

Per [ADR-015](./docs/adr/015-hermes-upstream-patches.md), 4 additive ABC patches were drafted for `NousResearch/hermes-agent`:
- 001 preassemble — **FILED** ([PR #24949](https://github.com/NousResearch/hermes-agent/pull/24949))
- 002 register_command forwarding — drafted (low priority)
- 003 engine.ingest hook — drafted (workaround in place)
- 004 cache-token forwarding — drafted (graceful degrade)

Track status weekly: `gh pr view 24949 --repo NousResearch/hermes-agent --json state,reviewDecision,mergedAt`

## What NOT to do

- **Don't use `--no-verify`** on commits. If pre-commit fails, fix the root cause.
- **Don't bypass schema-diff CI.** Data integrity depends on it.
- **Don't merge a PR without a fresh-context Pair Reviewer approval** (the dry-run pattern is the standard).
- **Don't share a working tree across 2+ parallel agents.** Use `git worktree add` per agent.
- **Don't introduce new direct deps without exact pinning** (ADR-006) and updating `uv.lock`.
- **Don't change an ADR's decision without writing a new ADR that supersedes it.** ADRs are append-only.

## On compaction risk

This is a 122-issue, 5–6 month project. Sessions WILL hit compaction. The resume protocol above is the recovery path:
- All real progress lives in Git, not chat context
- STATUS.md is the projection ("where are we"); Git is the truth
- Per-issue specs in `epics/*/issues/*.md` contain everything an executor needs

If a session resumes mid-issue (uncommitted work on a branch): `git status` + `git branch --show-current` answer "is there partial work?" Trust Git over memory.
