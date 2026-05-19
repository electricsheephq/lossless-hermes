# Voyage hybrid-retrieval recall benchmark — 2026-Q2

> Reproduction of the Phase-A spike's **+52.5pp paraphrastic recall lift** of hybrid Voyage retrieval over FTS-only, ported to Python (issue 09-08, the terminal Epic-09 artifact).

This is a generated report — re-run `scripts/benchmark_voyage_recall.py` to regenerate it. The FTS-only baseline is measured and reproducible **with no API key**; the hybrid arm requires a live `VOYAGE_API_KEY` (see [Reproduction recipe](#reproduction-recipe)).

## Acceptance verdict

**Status: HYBRID ARM PENDING.** The `fts_only` baseline below is measured and reproducible offline. The `hybrid` arm — and therefore the paraphrastic-lift acceptance check — requires a live `VOYAGE_API_KEY`, which was not available in the run environment. See [Live hybrid run PENDING](#live-hybrid-run-pending) for the exact command to complete the measurement.

The acceptance gate (paraphrastic lift within `[+47.5pp, +57.5pp]`) is **operator-gated** on that run.

## Result — per-stratum recall lift

TS-baseline columns are the Phase-A spike (`lossless-claw/docs/v4.1/PR_DESCRIPTION.md` §"Why Voyage embeddings", LCM commit `1f07fbd`). Py columns are this run. recall@5 is the headline figure — it matches the spike's "top-5 relevance grading".

| Stratum | n | TS FTS-only R@5 | TS Hybrid R@5 | TS lift | Py FTS-only R@5 | Py Hybrid R@5 | Py lift |
|---|---|---|---|---|---|---|---|
| fts-easy | 14 | 40.5% | 69.0% | +28.5pp | 88.1% | _pending_ | _pending_ |
| fts-medium | 9 | _not graded_ | _not graded_ | — | 20.4% | _pending_ | _pending_ |
| paraphrastic | 8 | 5.0% | 57.5% | +52.5pp | 0.0% | _pending_ | _pending_ |

_Py Hybrid / Py lift show `pending` — the hybrid arm did not run in this environment (no `VOYAGE_API_KEY`)._

## Measured FTS-only baseline (offline, reproducible)

Pure SQLite FTS5 — no API. These numbers are deterministic given the synthetic corpus + the eva-baseline-v2 fixture; anyone can reproduce them by running this script with no environment setup.

**fts_only — recall@K by stratum**

| Stratum | n | R@1 | R@5 | R@10 | R@20 | R@50 | MRR |
|---|---|---|---|---|---|---|---|
| fts-easy | 14 | 0.798 | 0.881 | 0.964 | 0.964 | 0.964 | 0.940 |
| fts-medium | 9 | 0.130 | 0.204 | 0.222 | 0.222 | 0.222 | 0.222 |
| paraphrastic | 8 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| **overall** | 31 | 0.398 | 0.457 | 0.500 | 0.500 | 0.500 | 0.489 |

- **paraphrastic** R@5 = 0.0% — the FTS-only weakness the hybrid arm exists to fix. The Phase-A spike measured 5.0% here; the Path-B synthetic corpus's paraphrastic queries are authored with *zero* surface-token overlap, so FTS5 finds nothing (0%). 0% vs 5% is within the noise floor and, if anything, makes the corpus a slightly *harder* paraphrastic test than the spike's.
- **fts-easy** R@5 = 88.1% — the spike measured 40.5%. This gap is **expected and explained**: the spike ran against Eva's real ~2.6 GB snapshot DB; this benchmark runs against the deterministic `v41-test-corpus` synthetic fixture (Path B), whose fts-easy queries were authored with literal phrase overlap against known leaves. The synthetic fts-easy stratum is therefore an *easier* FTS target than the spike's real-corpus stratum. The paraphrastic stratum — the load-bearing +52.5pp line — is unaffected by this, because paraphrastic recall on FTS-only is ~0% on *either* corpus.

## Methodology

- **Corpus:** `tests/fixtures/test_corpus.py` — the Python port of `lossless-claw/test/fixtures/v41-test-corpus.ts` (commit `1f07fbd`). A deterministic synthetic SQLite DB: 54 leaf summaries + 2 condensed summaries across 5 conversations. No PII; reproducible byte-for-byte. The spike used Eva's private snapshot DB (Path A); Path B was taken here because that snapshot is unavailable — see the 09-05 / 09-08 specs.
- **Query set:** `eva-baseline@v2` — 31 stratified queries (14 fts-easy / 9 fts-medium / 8 paraphrastic), built by `tests/fixtures/eva_baseline_v2.py::build_eva_baseline_v2()` and registered into `lcm_eval_query_set` via `register_query_set`.
- **FTS-only adapter:** wraps the Epic-06 `lcm_grep` FTS5 path — a `mode="full_text"` query against the `summaries_fts` FTS5 virtual table via `SummaryStore.search_summaries`. The adapter is `run_live_eval._build_fts_search` — *not duplicated here*.
- **Hybrid adapter:** wraps the Epic-05 `run_hybrid_search` — FTS5 + Voyage semantic embeddings union, then Voyage `rerank-2.5`. The adapter is `run_live_eval._build_live_adapters`'s hybrid arm — *not duplicated here*. Embeddings are backfilled for every summary via `embeddings/backfill.py::tick_embedding_backfill` before the hybrid run.
- **K values:** [1, 5, 10, 20, 50]. recall@5 is the headline (the spike graded top-5).
- **Per-query timeout:** the `run_recall_eval` default (30s, clamped ≥100ms) — Wave-4/5/9 scar tissue, see `eval/recall.py`.
- **Judge ensemble:** none. v4.1 first cut is recall-only; the synthesis-quality judge (`eval/judge.py`) is deferred. No Anthropic spend.
- **Lift computation:** a direct per-stratum subtraction (`hybrid.by_stratum[s].mean_recall_at_k[5] - fts.by_stratum[s].mean_recall_at_k[5]`) — `compute_lift` in this script. Deliberately *not* `compute_drift`, which compares same-mode runs across time.
- **Recorded runs:** both arms are written to `lcm_eval_run` via `record_eval_run` (`mode="fts_only"` run `evalrun_mpc7tq5c_6b9rcqb2`; the hybrid run is recorded only when the hybrid arm runs). `compute_drift` is then run on each — it completes without error even on the baseline run (returns `prior_run_id=None`).

## Cost breakdown

- **This run:** $0.0000 — the FTS-only arm makes no API calls. The hybrid arm did not run.
- **Expected hybrid-arm cost:** ~$0.03 (31 short query embeds + a few hundred rerank candidates, well under 100K tokens at $0.18/1M). The 09-08 spec's hard ceiling is < $0.50/run. The Phase-A spike's one-time cost was $0.58 total.

## Reproduction recipe

**FTS-only baseline (no API key — fully reproducible):**

```bash
python scripts/benchmark_voyage_recall.py \
    --out docs/benchmarks/voyage-recall-2026-q2.md
```

Writes the FTS baseline + a PENDING hybrid section. The synthetic corpus is seeded in-memory by default; pass `--db <path>` to persist it.

**Full benchmark including the hybrid arm (requires a key):**

```bash
export VOYAGE_API_KEY=<your-voyage-key>
python scripts/benchmark_voyage_recall.py \
    --out docs/benchmarks/voyage-recall-2026-q2.md
```

With the key set, the hybrid arm runs: it backfills Voyage embeddings for every summary, runs the hybrid recall eval, records the `hybrid` run, and fills the Py Hybrid / Py lift columns + the acceptance verdict above.

## Run metadata

- **Benchmark ran:** 2026-05-19 05:51 UTC
- **Wall-clock:** 0.00s
- **Python:** 3.13.12 (Darwin arm64)
- **Voyage embedding model:** `voyage-4-large`
- **Voyage rerank model:** `rerank-2.5`
- **Voyage client:** `lossless_hermes.voyage.client` — httpx pin `httpx[socks]==0.28.1` (Spike 004)
- **TS source commit:** `1f07fbd` (branch `pr-613`)
- **Hybrid arm:** SKIPPED (no VOYAGE_API_KEY)

## Deviation from the +52.5pp baseline

Exact reproduction of +52.5pp is impossible — the 09-08 spec allows a ±5pp band. Sources of legitimate deviation:

1. **Corpus difference (Path A vs Path B).** The spike measured against Eva's real snapshot DB; this benchmark uses the deterministic `v41-test-corpus` synthetic fixture. This mainly shifts the *fts-easy* baseline (synthetic fts-easy queries have cleaner literal overlap → higher FTS recall). The *paraphrastic* stratum is robust to it: FTS-only paraphrastic recall is ~0% on either corpus, so the lift is dominated by the hybrid arm's absolute recall.
2. **Voyage model-version drift.** `voyage-4-large` / `rerank-2.5` weights may differ between the spike and this run — third-party models we cannot pin.
3. **float32 vs float64 precision.** Embeddings are stored as float32 in vec0 but JSON round-trips as float64 (Spike 004 §"Remaining 5% risk" #1).
4. **Tokenizer drift.** If Voyage updates its tokenizer, identical input text yields different token boundaries.

Per the 09-08 spec §Mitigation: a Python paraphrastic lift ≥30pp (the original decision gate) still justifies shipping Voyage in v0.1.0 even if it is not exactly +52.5pp. A lift *below* 30pp is a real port defect and blocks the issue — file a child defect against #05-* or the Voyage client.

## Live hybrid run PENDING

**This benchmark's hybrid arm has not yet run.** It requires a live `VOYAGE_API_KEY`, which was not provisioned in the environment that generated this report. The +52.5pp number below is the **TS-baseline target**, NOT a measured Python result — it must not be cited as reproduced until the run below completes.

**What is verified (offline, in this report):**

- The `v41-test-corpus` Python port seeds the deterministic corpus (54 leaves) — verified by `tests/fixtures/test_test_corpus.py`.
- The eva-baseline-v2 query set registers + round-trips.
- The **FTS-only baseline is fully measured** — see the table above.
- The benchmark harness — corpus seed → query-set register → recall eval → `record_eval_run` → `compute_drift` → `compute_lift` → this report — runs end-to-end and is covered by `tests/benchmarks/test_voyage_recall_benchmark.py` with the Voyage seam mocked.

**What is operator-gated (the remaining step):**

Provision a `VOYAGE_API_KEY` and run the full benchmark:

```bash
export VOYAGE_API_KEY=<voyage-key>
python scripts/benchmark_voyage_recall.py \
    --out docs/benchmarks/voyage-recall-2026-q2.md
```

That regenerates this report with the hybrid arm measured, fills the Py Hybrid / Py lift columns, and resolves the acceptance gate: the paraphrastic lift must land within `[+47.5pp, +57.5pp]` (TS-baseline +52.5pp ±5pp) for the port to be ruled faithful.

This run also happens automatically on any retrieval-touching PR via the `live-eval` CI workflow (`scripts/run_live_eval.py`) once the `VOYAGE_API_KEY` repo secret is configured — that workflow exercises the same hybrid adapter against the same query set.

---

_Generated by `scripts/benchmark_voyage_recall.py` on 2026-05-19 05:51 UTC. Issue 09-08. TS source commit `1f07fbd`._
