# Ledger

> **Wave cost + velocity ledger.** One row per wave (a wave = batch of issues executed in one Claude session or focused multi-session push).
>
> **Throttle trigger:** wave running >25% over estimate at midpoint → stop dispatching, append a `## Re-baseline notes` block, re-estimate before continuing.

## Cumulative

| Metric | Estimated | Actual | Variance |
|---|---:|---:|---:|
| Total agent-hours | 610–830 | ~1 (W0 dry-run) | — |
| Total cost USD | $40K–$60K | ~$15 (est, W0 + W0e dry-run) | — |
| Total issues merged | 122 | 1 | — |
| Total PRs opened | 122+ | 1 | — |

## Per-wave

| Wave | Started | Closed | Issues | Est tokens | Actual tokens | Est cost USD | Actual cost USD | $/issue | Notes |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| W0 | 2026-05-13 | 2026-05-13 | 5 spikes + dry-run on 00-01 | ~500K | ~480K (est) | $20–40 | ~$15 (est) | n/a | 0a/0b/0c/0d/0e all green; upstream PR #24949 filed; PR #1 merged |
| W1 | 2026-05-13 | 2026-05-13 (in progress; 7/8) | 8 (00-02..00-08) | ~1.4M | ~1.5M (est) | $50–80 | ~$50 (est) | ~$8 (est) | 6 parallel Executors + 6 Reviewers; 1 fix-forward; 1 rebase conflict resolved via `--theirs`; key operational lesson: per-worktree isolation required for parallel agents |

## Per-issue

| Issue | Wave | PR | Status | Tokens (Executor) | Tokens (Reviewer) | Notes |
|---|---|---|---|---:|---:|---|
| 00-01 | W0 (dry-run) | [#1](https://github.com/electricsheephq/lossless-hermes/pull/1) | ✅ merged 2026-05-13T10:38:48Z | ~103K | ~101K | 22 deps installed, 3 atomic commits, 15/15 AC ticked, 97% confidence APPROVE |
| 00-05 | W1 | [#3](https://github.com/electricsheephq/lossless-hermes/pull/3) | ✅ merged 2026-05-13T11:04:39Z | ~118K | ~74K | Used isolated worktree; 81-LOC bridge + 135-LOC tests; ImportError fallback verified in subprocess |
| 00-07 | W1 | [#7](https://github.com/electricsheephq/lossless-hermes/pull/7) | ✅ merged 2026-05-13T11:04:45Z | ~171K | ~78K | Resolved spec-vs-prompt tension (empty v0 model per issue spec); 12 tests pass |
| 00-04 | W1 | [#4](https://github.com/electricsheephq/lossless-hermes/pull/4) | ✅ merged 2026-05-13T11:04:52Z | ~117K | ~97K | 5 matcher classes + 6 fixtures; 19/19 tests pass on Python 3.14 |
| 00-03 | W1 | [#2](https://github.com/electricsheephq/lossless-hermes/pull/2) | ✅ merged 2026-05-13T11:04:58Z | ~93K | ~85K | Pinned hook revs; `pre-commit run --all-files` clean |
| 00-02 | W1 | [#5](https://github.com/electricsheephq/lossless-hermes/pull/5) | ✅ merged 2026-05-13T11:05:?Z | ~181K | ~87K | 3 commits with 2 fix-forwards; 6/6 CI cells green; scaffolding scope-narrowings documented |
| 00-08 | W1 | [#6](https://github.com/electricsheephq/lossless-hermes/pull/6) | ✅ merged 2026-05-13T11:09:56Z | ~154K | ~87K | README rewrite + CONTRIBUTING; rebased + force-pushed after PR #5; required README conflict resolution via --theirs |

---

## Cost-tracking source

Anthropic API spend is computed end-of-wave from Claude Code session logs:

```bash
# At end of each wave, append a new row to the per-wave table.
# Helper script (to be written in Wave 0a alongside schema-diff):
# scripts/wave_cost_summary.sh <wave_id>
```

Until that helper exists, each wave's actual cost is recorded by hand at wave close, drawing from the Anthropic billing dashboard.

## Re-baseline notes

_(none yet)_
