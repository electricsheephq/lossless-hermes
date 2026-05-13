# ADR-030: PR #628 (stub-tier) inclusion strategy

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 90%
**Supersedes:** —
**Superseded by:** —

## Context

`lossless-claw` has two important branches:

- **`pr-613`** — the v4.1 omnibus PR, frozen at commit `1f07fbd` (`docs/reference/lcm-source-map.md` line 5). This is the verified target for the Python port — all porting guides (storage, engine, tests-and-config) and the source map describe `pr-613`'s state.
- **`main`** — a few commits ahead. Commit `13780e9` (PR #628, merged 2026-05-11) adds a "stub-tier" feature: a new storage tier for tool-result blobs that externalizes large payloads to disk with a stub left in the message. This includes:
  - Source: ~110 LOC across `src/db/migration.ts` (~32 LOC adding an `is_externalized` column on `message_parts`), `src/store/conversation-store.ts` (~46 LOC for the externalize / hydrate paths), and supporting helpers (~32 LOC).
  - Migration script: `scripts/lcm-blob-migrate.mjs` (365 LOC) — the offline migration tool that scans existing rows and externalizes oversized blobs.
  - Tests: ~380 LOC across new and modified test files.
  - Total: ~875 LOC of new behavior across source + migration + tests.

Storage porting guide flags this explicitly (`docs/porting-guides/storage.md` lines 7–8, 415–430):

> Note: #628 (stub-tier + `scripts/lcm-blob-migrate.mjs`) is on `main` (commit `13780e9`) but **NOT** in `pr-613`. See "Migration script #628" below.

And later in the same doc (lines 415–418):

> The migration script `scripts/lcm-blob-migrate.mjs` (365 LOC) was added on **main** in commit `13780e9` (PR #628, merged 2026-05-11). It is **NOT** present on the `pr-613` branch we're porting.
>
> - **Option A — port pr-613 first, defer #628 stub-tier work to Epic later.** Cleaner scope: the v4.2 stub-tier externalization adds 32 lines to `migration.ts` (one `is_externalized` column on message_parts), 46 lines to `conversation-store.ts`, plus the 365-line external script.

The constraint forcing a choice: do we port `pr-613` and `#628` together as v0.1.0, or port `pr-613` as v0.1.0 and `#628` as v0.2.0?

## Options considered

### Option A: v0.1.0 = pr-613 only; v0.2.0 = pr-613 + #628 stub-tier

- Description: Ship v0.1.0 as a verbatim port of `pr-613` (v4.1 omnibus). Defer stub-tier to v0.2.0 as a small follow-up release. v0.2.0 ports #628's added LOC (~110 source + 380 tests + 190 LOC migration script port).
- Pros:
  - **Smaller v0.1.0 scope.** The port effort is already ~120–160 hours for storage alone (`docs/porting-guides/storage.md` line 36) + ~80–120 hours for engine + similar for other subsystems. Adding stub-tier on top of `pr-613` ports doubles cognitive load for v0.1.0 reviewers (two parallel evolutions of the same files).
  - **Stub-tier is isolated.** PR #628's scope is bounded — one new column, two write-path call sites, one migration script. Easy to land cleanly as a focused follow-up.
  - **v0.2.0 has full pseudocode already.** Storage porting guide already documents the externalize/hydrate path; #628's diff is reviewable in TS. The Python port is mechanical given the v0.1.0 foundation.
  - **Production users of OpenClaw who never enabled stub-tier are unaffected.** Most OpenClaw deployments are on `pr-613` (the released production branch); adopting `lossless-hermes` v0.1.0 means no behavior change for them.
  - **Sequencing isolates risk.** If a bug ships in v0.1.0, it's a `pr-613` bug; the user can roll back without losing stub-tier. If a bug ships in v0.2.0, it's likely in the stub-tier delta; bisection is small.
- Cons:
  - **Two releases instead of one.** Users running on `main` (with stub-tier already in their `lcm.db`) need v0.2.0 to import cleanly. They wait one release cycle.
  - **README and ROADMAP must explicitly call out** that stub-tier is v0.2.0; otherwise users assume parity with `main`.
- Evidence cited:
  - `docs/porting-guides/storage.md` §"Migration script #628" lines 415–430 — explicitly recommends Option A.
  - `docs/porting-guides/storage.md` lines 7–8 — the pin is `pr-613`, with #628 explicitly NOT included.

### Option B: v0.1.0 = pr-613 + #628 stub-tier (single release)

- Description: Ship v0.1.0 as a port of `pr-613` with #628 merged in. All-in-one release.
- Pros:
  - One release; users running on `main` get parity immediately.
  - No follow-up release coordination.
- Cons:
  - **Doubles cognitive load for v0.1.0 reviewers.** The porting docs are all written against `pr-613`; reviewers must additionally validate the #628 delta against an undocumented baseline.
  - **Mixed-source provenance.** `git blame` against the Python port shows commits where some lines came from `pr-613` and some from `main`. Diff-readability suffers.
  - **Bug surface expands.** If a v0.1.0 user reports a bug, the first triage step is "is this from `pr-613` or from `#628`?" — a wider search space.
  - **The OpenClaw `lcm.db` migration story is complicated.** Existing users with `pr-613`-vintage DBs need to migrate via `lossless-hermes import-openclaw` (ADR-025), then re-run `lcm-blob-migrate` in Python form. Sequencing two migration steps is feasible but harder to test.
  - **Spec-vs-implementation drift.** All porting guides target `pr-613`; #628 is documented separately. A v0.1.0 that mixes both has no single source of truth.

### Option C: Port `main` (with stub-tier) as v0.1.0

- Description: Re-pin the port to `main` (commit `13780e9` or later). Treat `pr-613` as historical.
- Pros: Future-forward; no follow-up release needed.
- Cons:
  - **Throws away the porting-guide work.** Every guide is pinned to `pr-613`. Re-pinning requires re-auditing all sources, re-running all spike work, re-mapping LOC counts. Several months of foundation work would need a refresh pass.
  - `pr-613` is the production branch most operators run; targeting `main` ships a port of a non-production version.
  - High risk of additional drift between `main` and the port if `main` continues to advance.

## Decision

Chosen: **Option A — v0.1.0 = pr-613; v0.2.0 = pr-613 + #628 stub-tier**.

Release plan:

- **v0.1.0 (target: first Python release).** Verbatim 1:1 port of `pr-613` (commit `1f07fbd`) covering all 92 source files (per `docs/reference/lcm-source-map.md`), 8 `lcm_*` tools, 25 `/lcm` slash subcommands, full storage layer, full engine, full retrieval + synthesis + embeddings + extraction. Coverage: 80% line. Test count target: ~1,595 (parametrize-adjusted; per ADR-028).
- **v0.2.0 (target: ~2 months after v0.1.0 stabilizes).** Adds `#628`'s stub-tier:
  - Source delta: ~110 LOC across `db/migration.py` (32 LOC: `is_externalized` column + migration step), `store/conversation.py` (~46 LOC: externalize/hydrate paths), and supporting helpers (~32 LOC).
  - Migration script: port `scripts/lcm-blob-migrate.mjs` (365 LOC) to `scripts/hermes_blob_migrate.py` (~190 LOC — Python's smaller idiom benefits apply here, plus the script reuses the migrated storage helpers).
  - Tests: ~380 LOC of new test cases under `tests/test_stub_tier.py` and additions to `tests/test_migration.py`.
  - CLI: `lossless-hermes migrate-blobs` (owner-gated, similar to `import-openclaw`).

## Rationale

`pr-613` is the documented, spike-validated target. All porting guides (`docs/porting-guides/storage.md`, `engine.md`, `tests-and-config.md`) describe `pr-613`'s state in detail. Adding `#628` on top requires layering a delta on top of a port-in-progress — exactly the kind of decision compounding that makes review hard.

`#628` is small, well-isolated, and reviewable in isolation. Its scope (one new column, two call sites, one migration script) means v0.2.0 is a focused, low-risk follow-up. The storage porting guide already recommends this exact sequencing (`docs/porting-guides/storage.md` line 418 — "port pr-613 first, defer #628 stub-tier work to Epic later").

The cost of two releases is small. Users running OpenClaw v4.1 (the `pr-613`-equivalent production version) gain parity in v0.1.0. Users running the newer `main`-vintage OpenClaw with stub-tier enabled gain parity in v0.2.0. The waiting period for the latter group is one release cycle, not a permanent regression.

Mixing both (Option B) would double the review surface for v0.1.0 and complicate the migration story. Re-pinning to `main` (Option C) would throw away months of porting-guide work and target a non-production OpenClaw version.

## Consequences

- **v0.1.0 release scope** is exactly what's in `docs/porting-guides/storage.md`, `docs/porting-guides/engine.md`, `docs/porting-guides/tests-and-config.md`, and `docs/reference/lcm-source-map.md`. No surprises; no late-added scope.
- **README must call out stub-tier deferral.** Suggested wording:

  > **Stub-tier (tool-result externalization) is not in v0.1.0.** It is planned for v0.2.0. If you run OpenClaw with stub-tier enabled (LCM `main` branch, commit `13780e9` or later), see ROADMAP.md for the v0.2.0 timeline. Your existing `lcm.db` is forward-compatible — the `is_externalized` column will be added by v0.2.0's migration ladder without re-ingest.

- **ROADMAP.md** documents v0.2.0:

  > **v0.2.0 — stub-tier (PR #628 port)**
  >
  > Adds `is_externalized` column on `message_parts` and the offline blob-migration CLI. Target: ~2 months after v0.1.0 stabilizes.
  >
  > - `db/migration.py`: new migration step `addIsExternalizedToMessageParts` at `algorithm_version=1` (per ADR-026).
  > - `store/conversation.py`: externalize/hydrate paths.
  > - `scripts/hermes_blob_migrate.py`: port of `scripts/lcm-blob-migrate.mjs`.
  > - `lossless-hermes migrate-blobs` CLI command (owner-gated; mirror of `import-openclaw`).

- **`lossless-hermes import-openclaw` (ADR-025) tolerates stub-tier rows.** If an operator's source `lcm.db` has the `is_externalized` column from a `main`-vintage OpenClaw install, v0.1.0's migration ladder is a no-op on that column (idempotent — per ADR-026). The Python code simply doesn't use the column until v0.2.0 adds the externalize/hydrate paths. Read/write behavior is unchanged in v0.1.0 because the column stays NULL on new rows.
- **`pr-613` pin in CI.** A scheduled CI job validates that the Python port's behavior matches `pr-613` (TS) on a shared fixture corpus. v0.1.0 must not drift from `pr-613`. v0.2.0's CI adds the #628 baseline.
- **Spike work for v0.2.0** (recommended before starting): a one-day spike to validate that `lcm-blob-migrate.mjs` ports cleanly to Python with the shared storage helpers — confirm that the externalize directory layout, blob-naming scheme, and SHA-based deduplication carry over without surprises. Result documented in `docs/spike-results/004-blob-migrate.md`.
- **Invariant:** v0.1.0 is a `pr-613`-only port. Any v0.1.x patch must be a backport from `pr-613` (or a bug-fix; never a `main`-only feature). New features land in v0.2.x.
- **Invariant:** v0.2.0 adds only what `#628` adds. It does not bundle other `main`-only changes unless explicitly scoped in a future ADR.

## Open questions / 5% uncertainty

1. **OpenClaw `main` advances during v0.1.0 development.** If `main` lands more PRs beyond `#628` between now and the v0.1.0 ship, we will face the same question for each: defer to v0.2.x or pull in. Default: defer; explicit ADR for any exception.
2. **Forward-compat of `lcm.db`.** If an operator's source DB has `is_externalized` column (from a `main`-vintage OpenClaw), v0.1.0 ignores it. If they later use stub-tier behavior in OpenClaw and then re-import to v0.1.0, the new column data is preserved but unused. v0.2.0's migration picks it up. Document this lifecycle.
3. **#628 has its own scar tissue.** During the v0.2.0 port, audit `#628` for Wave-N markers (ADR-029) — if the stub-tier path has any race fixes, retry-storm caps, etc., they need inline comments in the Python port. Schedule audit as part of v0.2.0 planning.
4. **What if stub-tier becomes critical before v0.2.0 ships?** If a v0.1.0 user reports a real production need (e.g. they keep hitting tool-result token limits and need externalization), we can either accelerate v0.2.0 or backport stub-tier as v0.1.x. Default to acceleration; backporting violates the "v0.1.x is pr-613-only" invariant.
5. **Migration-script naming.** `lcm-blob-migrate.mjs` is the TS name. We use `hermes_blob_migrate.py` per Python conventions. The CLI command is `lossless-hermes migrate-blobs`. Confirm during v0.2.0 design that these names are not pre-empted by other Hermes commands.
