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
**Resolution:** _(pending 09-08 executor report + v0.1.0 release-gate review)_

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
