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
| W1 | 2026-05-13 | — | 7 (00-02..00-08) | ~1.4M | — | $50–80 | — | ~$10 (est) | Epic 00 remaining issues, parallel fan-out after 00-01 |

## Per-issue

| Issue | Wave | PR | Status | Tokens (Executor) | Tokens (Reviewer) | Notes |
|---|---|---|---|---:|---:|---|
| 00-01 | W0 (dry-run) | [#1](https://github.com/electricsheephq/lossless-hermes/pull/1) | ✅ merged 2026-05-13T10:38:48Z | ~103K | ~101K | 22 deps installed, 3 atomic commits, 15/15 AC ticked, 97% confidence APPROVE |

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
