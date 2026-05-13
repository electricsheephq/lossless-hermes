# Ledger

> **Wave cost + velocity ledger.** One row per wave (a wave = batch of issues executed in one Claude session or focused multi-session push).
>
> **Throttle trigger:** wave running >25% over estimate at midpoint → stop dispatching, append a `## Re-baseline notes` block, re-estimate before continuing.

## Cumulative

| Metric | Estimated | Actual | Variance |
|---|---:|---:|---:|
| Total agent-hours | 610–830 | 0 | — |
| Total cost USD | $40K–$60K | $0 | — |
| Total issues merged | 122 | 0 | — |
| Total PRs opened | 122+ | 0 | — |

## Per-wave

| Wave | Started | Closed | Issues | Est tokens | Actual tokens | Est cost USD | Actual cost USD | $/issue | Notes |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| W0 | 2026-05-13 | — | 5 spikes | ~500K | TBD | $20–40 | TBD | n/a | scaffolding + spikes; not real issue work |

## Per-issue (auto-populated by `scripts/refresh_status.py`)

| Issue | Wave | PR | Status | Tokens | Cost USD | Agent-hours |
|---|---|---|---|---:|---:|---:|
| _(none merged yet)_ | | | | | | |

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
