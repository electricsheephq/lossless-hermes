---
name: Port issue
about: CI live-eval workflow gated on VOYAGE_API_KEY + ANTHROPIC_API_KEY
title: '[epic-09] eval: GH Action live-eval workflow on retrieval-touching PRs'
labels: 'port, epic-09-eval, ci'
---

## Source (TypeScript)

- File: `.github/workflows/ci.yml` (LCM v4.1, `pr-613` HEAD `1f07fbd`)
- Lines: ~50 LOC; LCM's CI is minimal (Node 22 + `npm test` + smoke-against-openclaw-latest).
- **No live-eval workflow exists upstream.** LCM relied on Eva's manual `scripts/v41-qa-runner.mjs` runs against her local DB snapshot. This issue creates the equivalent automation in the Python port — the workflow is new infrastructure, not a TS-line port.
- Reference CI shape: `docs/porting-guides/tests-and-config.md` §"Recommended Hermes CI" lines 498–531 (which already sketches `live-voyage` as a separate job).

## Target (Python)

- File: `.github/workflows/live-eval.yml`
- Estimated LOC: ~80 (workflow YAML)
- Supporting Python: `scripts/run_live_eval.py` (~120 LOC) — orchestrates `register_query_set` → `run_recall_eval` (FTS-only + hybrid adapters) → `record_eval_run` → `compute_drift` → `per_stratum_drift` → emit GH-Actions summary + PR comment.

## What this issue covers

A GitHub Actions workflow that runs the recall+drift suite against `eva-baseline-v2` whenever a PR touches **retrieval-relevant** code. Behavior:

1. **Trigger:** `pull_request` and `push to main` with `paths:` matching:
   ```yaml
   paths:
     - 'src/lossless_hermes/embeddings/**'
     - 'src/lossless_hermes/synthesis/**'
     - 'src/lossless_hermes/compaction.py'
     - 'src/lossless_hermes/tools/grep.py'
     - 'src/lossless_hermes/eval/**'
     - 'tests/fixtures/eva_baseline_v2.py'
     - '.github/workflows/live-eval.yml'
     - 'scripts/run_live_eval.py'
   ```
2. **Auth gate:** the job is skipped (not failed) if either `VOYAGE_API_KEY` or `ANTHROPIC_API_KEY` is missing — e.g., forks without secrets. Implemented as a `permissions:` + `if: secrets.VOYAGE_API_KEY != ''` gate at the job level.
3. **Concurrency control:** `concurrency: { group: live-eval-${{ github.ref }}, cancel-in-progress: true }` so push-after-push doesn't waste API quota.
4. **What it runs:**
   - Builds the `eva-baseline-v2` corpus into a fresh `tests/eval-ci.db` (uses `test_corpus.py` seed + `register_query_set`).
   - Backfills embeddings for all summaries (call into Epic 05 `embeddings_backfill`).
   - Runs `run_recall_eval` with the **FTS-only** adapter → records `mode="fts_only"` run.
   - Runs `run_recall_eval` with the **hybrid (semantic + rerank)** adapter → records `mode="hybrid"` run.
   - Calls `compute_drift` against both runs (drift vs the prior CI run of the same mode, fetched from the cached `eval-baseline.db` artifact).
   - Calls `per_stratum_drift` and serializes per-stratum recall@K + cumulative delta as a markdown table.
5. **Outputs:**
   - **GH Actions step summary** (`$GITHUB_STEP_SUMMARY`) — full markdown table of per-stratum recall@5 + cumulative drift.
   - **PR comment** (`actions/github-script@v7` or `peter-evans/create-or-update-comment@v4`) — only on PRs, posts the drift summary as a sticky comment (updates the same comment on re-runs).
   - **Workflow failure** if `per_stratum_drift.any_stratum_regressed is True` AND the regressed stratum is `paraphrastic` (paraphrastic is the load-bearing differentiator — fts-easy drift is informational only).
6. **Baseline-DB caching:** before the job runs, restore `eval-baseline.db` from the workflow cache (`actions/cache@v4`, key `eval-baseline-v1`). The cache holds prior runs so `compute_drift` has something to compare against. After the run, save the updated DB back to the cache. First run on a branch has no prior → drift summary shows "(baseline established)" and the job is informational.
7. **Cost guardrail:** the workflow comments the **measured Voyage + Anthropic spend** for the run (parsed from the `total_tokens` returned by the embed/rerank calls and the judge's response usage). Hard ceiling: $0.50/run; abort if exceeded (defense-in-depth — single eval on 31 queries should cost ~$0.03).

## Workflow YAML skeleton

```yaml
name: live-eval

on:
  pull_request:
    paths:
      - 'src/lossless_hermes/embeddings/**'
      - 'src/lossless_hermes/synthesis/**'
      - 'src/lossless_hermes/compaction.py'
      - 'src/lossless_hermes/tools/grep.py'
      - 'src/lossless_hermes/eval/**'
      - 'tests/fixtures/eva_baseline_v2.py'
      - '.github/workflows/live-eval.yml'
      - 'scripts/run_live_eval.py'
  push:
    branches: [main]
    paths:
      # same paths
  workflow_dispatch: {}

concurrency:
  group: live-eval-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read
  pull-requests: write   # for the PR comment

jobs:
  live-eval:
    if: ${{ secrets.VOYAGE_API_KEY != '' && secrets.ANTHROPIC_API_KEY != '' }}
    runs-on: ubuntu-latest
    timeout-minutes: 15
    env:
      VOYAGE_API_KEY: ${{ secrets.VOYAGE_API_KEY }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      EVAL_COST_CEILING_USD: "0.50"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev]"
      - name: Restore baseline DB
        uses: actions/cache@v4
        with:
          path: eval-baseline.db
          key: eval-baseline-v1
      - name: Run live eval
        id: eval
        run: python scripts/run_live_eval.py --db eval-baseline.db --summary-md $GITHUB_STEP_SUMMARY
      - name: Save baseline DB
        if: always()
        uses: actions/cache/save@v4
        with:
          path: eval-baseline.db
          key: eval-baseline-v1-${{ github.sha }}
      - name: Comment on PR
        if: ${{ github.event_name == 'pull_request' && always() }}
        uses: peter-evans/create-or-update-comment@v4
        with:
          issue-number: ${{ github.event.pull_request.number }}
          comment-id: ${{ steps.eval.outputs.comment-id }}
          body-path: eval-report.md
          edit-mode: replace
```

## Dependencies

- **Depends on:** #09-01..#09-06 (all the eval modules), #09-05 (the fixture), Epic 05 (`embeddings/backfill.py` + hybrid adapter), Epic 06 (FTS-only adapter), Epic 07 (`LlmCall` adapter for judge if quality-eval is enabled).
- **Blocks:** none directly; #09-08 reuses the workflow to gate the benchmark PR.

## Acceptance criteria

- [ ] Workflow file at `.github/workflows/live-eval.yml`; lint-clean per `actionlint`.
- [ ] Triggers on PRs/pushes matching the documented `paths:` list (asserted manually by pushing a no-op commit touching `src/lossless_hermes/embeddings/store.py` and verifying the workflow fires; same exercise for `tools/grep.py`).
- [ ] Skips (does NOT fail) when secrets are missing (verified by running the workflow from a fork that has no `VOYAGE_API_KEY`).
- [ ] `concurrency: cancel-in-progress: true` so a push-replacement cancels the prior run.
- [ ] `timeout-minutes: 15` — abort runaway runs.
- [ ] `scripts/run_live_eval.py` runs both `fts_only` and `hybrid` modes against `eva-baseline-v2`, records both runs, and computes drift for both vs cached prior.
- [ ] **Cost guardrail:** sum of Voyage + Anthropic spend reported in the summary; if `> EVAL_COST_CEILING_USD` the script exits non-zero before submitting the final eval run (so partial data doesn't pollute the baseline DB).
- [ ] **Pass/fail rule:** `per_stratum_drift.by_stratum["paraphrastic"].cumulative_delta < -threshold` → workflow fails with a clear message. fts-easy/fts-medium regressions are informational only.
- [ ] **Sticky PR comment** — the workflow updates a single comment (identified by a magic marker like `<!-- live-eval-bot -->` in the body) rather than spamming with new comments on each push.
- [ ] **Baseline DB caching** works: first run on a branch has no prior, drift shows "(baseline established)"; second run shows real drift vs the first.
- [ ] Workflow summary table includes: per-stratum recall@5, per-stratum reciprocal-rank mean, per-stratum cumulative drift, total run cost in USD.
- [ ] Lives at `.github/workflows/live-eval.yml`; tests/CI for the script itself live at `tests/scripts/test_run_live_eval.py` (mocked Voyage + Anthropic via `respx` and `unittest.mock`).
- [ ] PR description cites: workflow path + script path + the issue number(s) being verified by the first paraphrastic-recall measurement.

## Tests

`tests/scripts/test_run_live_eval.py` (no live calls; everything mocked):

- Happy path: mocked FTS-only adapter returns 0.05 recall on paraphrastic; mocked hybrid adapter returns 0.575 → drift `+0.525`, paraphrastic stratum reports improvement, workflow exits 0.
- Regression path: hybrid recall drops to 0.30 (down from 0.575 baseline) → workflow exits non-zero with a paraphrastic-regression message.
- Cost-ceiling breach: mock returns `total_tokens` summing to >$0.50 of Voyage spend → script aborts before recording the final run; baseline DB unchanged.
- First-run path: no prior runs in baseline DB → drift summary says "(baseline established)" and exit 0.
- Auth-skip path: `VOYAGE_API_KEY=""` env → script exits 78 (`EX_CONFIG`) with a clear message; this maps to a skipped step in the workflow.

Manual / integration:
- Open a no-op PR touching `src/lossless_hermes/embeddings/store.py` (whitespace edit). Verify the workflow fires, posts a comment, and the comment updates on a follow-up push instead of spawning a new comment.

## Estimated effort

**4–6 hours.**

Breakdown: 1 h workflow YAML + actionlint, 2 h `run_live_eval.py` orchestration script, 1 h sticky-comment + cost-ceiling logic, 1 h mocked tests, 1 h manual integration round-trip on a sandbox PR.

## Confidence

**90%** — workflow YAML is standard, the Python orchestration script is composing already-tested modules. The 10% residual is in two places:

1. **The sticky-comment mechanism** depends on a third-party action (`peter-evans/create-or-update-comment@v4`) being available and stable; if it changes upstream, swap to `actions/github-script@v7` with a marker-based comment-find. Document the marker in the comment body so a manual replacement is trivial.
2. **Cost accounting** depends on Voyage + Anthropic returning accurate `total_tokens` in every response shape. Spike 004 verified Voyage; Anthropic's usage shape may differ between Sonnet and Opus and across model versions. Mitigation: track per-model usage; if a model returns `null` usage, log a warning and fall back to a conservative per-call estimate so we don't underbill the ceiling check.
