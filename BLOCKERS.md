# Blockers

> **Append-only dated queue of open decisions awaiting Claude judgment.**
>
> Resolved entries archive to `docs/decisions/YYYY-MM.md` after 30 days.

## Open blockers

_(none open as of 2026-05-19 — B-001 and B-002 resolved at the v0.1.0 release-gate review; see below)_

## Resolved (archive after 30 days)

### B-001 — 2026-05-19 — Live +52.5pp Voyage benchmark requires an unprovisioned API key
**Raised by:** project-lead session, Wave 6 / issue 09-08
**Blocks:** v0.1.0 release-gate verification item 7 ("Eval reproduces +52.5pp Voyage lift"). Did NOT block any code issue — Epic 09 shipped its harness regardless.
**Question:** The +52.5pp paraphrastic recall lift is measured by running the `hybrid` retrieval arm, which requires live Voyage embeddings (`VOYAGE_API_KEY`). Neither this execution environment nor the `electricsheephq/lossless-hermes` repo has `VOYAGE_API_KEY` / `ANTHROPIC_API_KEY` provisioned (the `live-voyage` + `live-eval` CI jobs correctly SKIP). The `fts_only` baseline is fully reproducible offline; the `hybrid` uplift number is not.
**Recommended action:** Ship v0.1.0 with the benchmark **harness** complete + verified, the `fts_only` baseline measured, and the live +52.5pp confirmation as a documented, single-command operator step gated on `VOYAGE_API_KEY` (`docs/benchmarks/voyage-recall-2026-q2.md`).
**Decision required:** Claude (autonomous) — gate-keeper role per the plan ("Eva-fixture / eval-reproduction judgment").
**Status:** resolved (2026-05-19)
**Resolution:** **ACCEPTED as a documented operator-gated v0.1.0 item.** Gate-keeper decision at the v0.1.0 release-gate review: the 09-08 deliverables are landed + reviewer-verified (PR #125) — the `v41-test-corpus` Python port, the benchmark harness (`scripts/benchmark_voyage_recall.py`), the published report (`docs/benchmarks/voyage-recall-2026-q2.md`), and the offline-measured `fts_only` baseline (paraphrastic recall@5 = 0.0%, the FTS-only weakness the hybrid arm addresses). The harness is exercised end-to-end by `tests/benchmarks/test_voyage_recall_benchmark.py` with the Voyage seam mocked. The live +52.5pp confirmation is a single documented command in the report's "Live hybrid run PENDING" section, gated on `VOYAGE_API_KEY`. This matches the plan's explicit "Proposed-with-shipped-fallback" tolerance (ADR-010 pattern) and the acknowledged R12 open question. Release-gate item 7 is satisfied as "harness reproduces; live run documented + pending key." v0.1.0 release notes carry this as a known limitation.

### B-002 — 2026-05-19 — Integration soak (24h+) requires a live Hermes deployment
**Raised by:** project-lead session, Wave 6 / v0.1.0 release gate
**Blocks:** v0.1.0 release-gate verification item 11 ("Integration soak: 24h+ run"). Did NOT block any code issue — all 122 port issues merged, CI matrix green.
**Question:** The plan's release gate calls for a 24h+ integration soak inside a running Hermes runtime — monitoring memory, token usage, embedding backfill, schema integrity, and an OpenClaw migration round-trip on a real 2.6GB Eva DB. An autonomous in-session run cannot host a 24h live process, and no real Eva DB or `VOYAGE_API_KEY` is provisioned in this environment.
**Recommended action:** Accept as a documented post-tag operator/maintainer step, same pattern as B-001.
**Decision required:** Claude (autonomous) — gate-keeper role.
**Status:** resolved (2026-05-19)
**Resolution:** **ACCEPTED as a documented operator-gated v0.1.0 item.** Gate-keeper decision at the v0.1.0 release-gate review: v0.1.0 ships with the full CI matrix green on `{macOS, ubuntu} × {3.11, 3.12, 3.13}`, ~4050 offline tests, the schema-diff byte-compat gate green (92/92 objects), and 28 offline `import-openclaw` tests (argparse, refusal, dry-run, migration, identity-hash sample-validation, idempotency, fixture round-trip). The soak is runtime-validation of an already-CI-verified codebase, not a code gap — and release-gate verification item 6 (Eva-DB 2.6GB round-trip) folds into the same operator step. Recommend the maintainer run the soak + real-DB round-trip against a staging Hermes deployment after the tag and before any production rollout; track residual findings as v0.2.0 issues. v0.1.0 release notes carry this as a known limitation.

---

## Entry format

```markdown
### B-NNN — YYYY-MM-DD — <one-line title>
**Raised by:** <agent type + issue/wave>
**Blocks:** <issue IDs, comma-separated, or "wave-level">
**Question:** <what needs deciding>
**Recommended action:** <agent's recommendation>
**Decision required:** Claude (autonomous) | human escalation
**Status:** open | resolved (YYYY-MM-DD)
**Resolution:** <if resolved, what was decided + commit/PR link>
```

## Judgment-call distinction

- **BLOCKERS.md** is for "I cannot proceed without a decision from Claude."
- For "I proceeded autonomously, here's the choice I made": record in the **commit message** of the issue (e.g., `Note: chose option A because <rationale>`). Don't add to this file.
- Durable design decisions get a new ADR; BLOCKERS is the *queue*, ADRs are the *record once accepted*.
