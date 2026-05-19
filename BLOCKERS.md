# Blockers

> **Append-only dated queue of open decisions awaiting Claude judgment.**
>
> Resolved entries archive to `docs/decisions/YYYY-MM.md` after 30 days.

## Open blockers

### B-001 — 2026-05-19 — Live +52.5pp Voyage benchmark requires an unprovisioned API key
**Raised by:** project-lead session, Wave 6 / issue 09-08
**Blocks:** v0.1.0 release-gate verification item 7 ("Eval reproduces +52.5pp Voyage lift"). Does NOT block any code issue — Epic 09 ships its harness regardless.
**Question:** The +52.5pp paraphrastic recall lift is measured by running the `hybrid` retrieval arm, which requires live Voyage embeddings (`VOYAGE_API_KEY`). Neither this execution environment nor the `electricsheephq/lossless-hermes` repo has `VOYAGE_API_KEY` / `ANTHROPIC_API_KEY` provisioned (the `live-voyage` + `live-eval` CI jobs correctly SKIP). The `fts_only` baseline is fully reproducible offline; the `hybrid` uplift number is not.
**Recommended action:** Ship v0.1.0 with the benchmark **harness** complete + verified, the `fts_only` baseline measured, and the live +52.5pp confirmation as a documented, single-command operator step gated on `VOYAGE_API_KEY` (`docs/benchmarks/voyage-recall-2026-q2.md`). This mirrors the ADR-010 "Proposed-with-shipped-fallback" pattern and the plan's acknowledged R12 open question — the port is correct and the harness is proven; the live number is an operator-provisioning step, not a code gap. Release-gate item 7 is satisfied as "harness reproduces; live run documented + pending key."
**Decision required:** Claude (autonomous) — gate-keeper role per the plan ("Eva-fixture / eval-reproduction judgment").
**Status:** open
**Resolution:** _(pending v0.1.0 release-gate review)_ — 09-08 executor side **complete** (PR `port/09-08-benchmark`): the `v41-test-corpus` Python port, the benchmark harness (`scripts/benchmark_voyage_recall.py`), and the published report (`docs/benchmarks/voyage-recall-2026-q2.md`) are all landed + verified; the `fts_only` baseline is **measured offline** (paraphrastic recall@5 = 0.0%, the FTS-only weakness the hybrid arm addresses). The `hybrid` arm + the live +52.5pp confirmation are a single documented operator-gated command in the report's "Live hybrid run PENDING" section, gated on `VOYAGE_API_KEY`. The harness is exercised end-to-end by `tests/benchmarks/test_voyage_recall_benchmark.py` with the Voyage seam mocked. This matches the recommended action exactly — release-gate item 7 is now satisfiable as "harness reproduces; live run documented + pending key," and the maintainer can close B-001 at the v0.1.0 gate review.

### B-002 — 2026-05-19 — Integration soak (24h+) requires a live Hermes deployment
**Raised by:** project-lead session, Wave 6 / v0.1.0 release gate
**Blocks:** v0.1.0 release-gate verification item 11 ("Integration soak: 24h+ run with healthy memory/token/embedding throughput"). Does NOT block any code issue — all 122 port issues are merged, CI matrix green.
**Question:** The plan's release gate calls for a 24h+ integration soak inside a running Hermes runtime — monitoring memory, token usage, embedding backfill, schema integrity, and an OpenClaw migration round-trip on a real 2.6GB Eva DB. An autonomous in-session run cannot host a 24h live process, and no real Eva DB or `VOYAGE_API_KEY` is provisioned in this environment.
**Recommended action:** Accept as a documented post-tag operator/maintainer step, same pattern as B-001. v0.1.0 ships with: full CI matrix green on `{macOS, ubuntu} × {3.11, 3.12, 3.13}`, ~4050 offline tests, schema-diff byte-compat gate green, and 28 offline `import-openclaw` tests (argparse, refusal, dry-run, migration, identity-hash sample-validation, idempotency, fixture round-trip). The soak is runtime-validation of an already-CI-verified codebase, not a code gap. Recommend the maintainer run the soak against a staging Hermes deployment after the tag and before any production rollout; track residual findings as v0.2.0 issues.
**Decision required:** Claude (autonomous) — gate-keeper role.
**Status:** open
**Resolution:** _(pending v0.1.0 release-gate review — recommend accepting as an operator-gated item; ships v0.1.0 with the offline test+CI evidence above)_

## Resolved (archive after 30 days)

_(none yet)_

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
