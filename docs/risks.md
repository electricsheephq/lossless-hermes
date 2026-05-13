# Risk Register: lossless-hermes

> **Audience:** anyone managing Phase-2 execution risk.
> **Updates:** when a risk transitions (probability/severity change, mitigation lands, escalation), append a dated line in the **Status log** section at the bottom and update the table.

## Risk categories

- **🟥 BLOCKER** — Without mitigation, the project cannot ship v0.1.0.
- **🟧 HIGH** — Mitigation has cost or schedule risk; Phase 2 must address before integration soak.
- **🟨 MEDIUM** — Manageable in flight; watch but no immediate action.
- **🟩 LOW** — Acknowledged; nothing to do today.

## Current risk register (sorted by severity)

| ID | Severity | Risk | Affected epic | Mitigation | ADR / spike |
|---|---|---|---|---|---|
| **R01** | 🟧 HIGH | Hermes `pre_llm_call` is append-only; no native hook to rewrite messages every turn — LCM's always-on assembly substitution depends on either an upstream `preassemble()` ABC patch landing, or a behavioral fallback that breaks session lineage | 03 | Pursue upstream PR ([ADR-015](./adr/015-hermes-upstream-patches.md) patch #1); accept "force compress=True" as experimental fallback gated by config flag | [Spike 002](./spike-results/002-hermes-pre-llm-call.md), [ADR-010](./adr/010-always-on-assembly.md) |
| **R02** | 🟧 HIGH | Phase 1 effort estimate is 600–900 hrs based on LOC + porting-guide hour breakdowns. Underestimating by 50% would push v0.1.0 from ~5mo to ~8mo single-engineer | all | Use the issue-level hour estimates as commitments; track velocity weekly; if Epic 01 (the easiest big one) overruns, replan | — |
| **R03** | 🟨 MEDIUM | `sqlite-vec` Python binding is pre-1.0 (`0.1.x`). API or behavior could shift before v0.2.0 release | 05 | Strict version pin (`sqlite-vec==0.1.9`); `apsw==3.53.1.0` documented fallback path | [Spike 001](./spike-results/001-sqlite-vec-python.md), [ADR-004](./adr/004-sqlite3-backend.md) |
| **R04** | 🟨 MEDIUM | Apple system Python (`/usr/bin/python3`) does NOT support `enable_load_extension`. Users running it directly will hit a confusing error | 00, 04 | Explicit doctor check + README warning + ADR-004 explicit unsupported list; suggest `uv` / Homebrew / pyenv | [Spike 001](./spike-results/001-sqlite-vec-python.md), [ADR-004](./adr/004-sqlite3-backend.md) |
| **R05** | 🟨 MEDIUM | LCM PR #613 (omnibus) is OPEN upstream as of 2026-05-13. If Martian merges or rewrites significantly, lossless-hermes either rebases or forks | 02, 04 | Pin source to commit SHA `1f07fbd` (current pr-613 head); fork from day 1 — don't depend on upstream merge | [Source map](./reference/lcm-source-map.md) |
| **R06** | 🟨 MEDIUM | Hermes is not on PyPI; install/CI matrix has to handle source-based host install | 00, 08 | Document `curl|bash` and `uv pip install -e` paths; ADR-007 captures the host-vs-PyPI position | [ADR-007](./adr/007-hermes-as-dependency.md) |
| **R07** | 🟨 MEDIUM | Linux not first-hand tested in Phase 1 (`sqlite-vec`, FTS5 trigram, Voyage round-trip). High likelihood of working based on bundled SQLite + manylinux wheels | 00 | First PR in Epic 00 must add `ubuntu-latest` to CI matrix; smoke test stdlib `sqlite3` + sqlite-vec + FTS5 trigram on Linux | [Spike 001](./spike-results/001-sqlite-vec-python.md) §"Remaining 5% risk", [Spike 005](./spike-results/005-sqlite3-fts5-trigram.md) §"Remaining 5% risk" |
| **R08** | 🟨 MEDIUM | `lcm_expand_query` tool depends on a sub-agent dispatch model that doesn't exist in Hermes the same way it does in OpenClaw | 06 | Deferred to v2 per ADR-012; main agents lose the convenience wrapper but `lcm_expand` primitive still works | [ADR-012](./adr/012-subagent-defer.md) |
| **R09** | 🟨 MEDIUM | The recall-policy prompt injection point (system vs user message) trades off model-side prompt quality vs prompt-cache hit rate. Phase 1 chose user message for cache preservation but didn't benchmark | 03 | Phase 2 must include a benchmark in Epic 03 acceptance criteria | [ADR-014](./adr/014-recall-policy-injection.md) |
| **R10** | 🟨 MEDIUM | Hermes-side upstream patches (preassemble, register_command, ingest, cache-token forwarding) require maintainer review and merge; cycle time is unknown | 03 | All 4 patches are additive + non-breaking; ship parallel workarounds for each | [ADR-015](./adr/015-hermes-upstream-patches.md) |
| **R11** | 🟨 MEDIUM | TS source uses `@mariozechner/pi-ai` dynamic import for LLM calls. Hermes's LLM client surface needs an adapter; "reasoning" parameter passthrough TBD | 04, 07 | Port `summarize.py` against a thin `lossless_hermes.llm_client` adapter; verify Hermes exposes reasoning_effort or equivalent | [Porting guide: assembler-compaction.md](./porting-guides/assembler-compaction.md) |
| **R12** | 🟨 MEDIUM | Eva's eval fixture (`eva-baseline-v2`, 31-query paraphrastic set) is not checked into upstream. Reproducing +52.5pp lift may require either recovering it from Eva's DB or rebuilding from `v41-test-corpus` | 09 | Two paths documented in issue 09-05 (recover vs rebuild). If rebuild, expected uplift may differ by ±5pp | [Epic 09 issue 09-05](../epics/09-eval/09-05-evaluation-fixtures.md), [Spike 004](./spike-results/004-voyage-python-client.md) |
| **R13** | 🟨 MEDIUM | Owner-gating is enforced by Hermes's upstream `SlashAccessPolicy` which we trust but don't own. If Hermes changes the gating contract, owner-only `/lcm` subcommands could become open or fail | 08 | Document trust assumption in ADR-013; startup warning if `allow_admin_from` unset | [ADR-013](./adr/013-owner-gating.md) |
| **R14** | 🟨 MEDIUM | `engine.ts` is 8,731 LOC — ADR-027 mandates splitting into mixin sub-modules. State synchronization risk between mixins | 02, 03, 04 | Single LCMEngine shell owns ALL state; mixins are organizational only | [ADR-027](./adr/027-engine-splitting.md) |
| **R15** | 🟩 LOW | TypeBox → JSON Schema translation by hand; ~6,500 LOC of tool schemas could have subtle differences | 06 | Lint test in Epic 06 issue 06-15 verifying every description string is verbatim | [ADR-016](./adr/016-typebox-translation.md), [Epic 06 issue 06-15](../epics/06-tools/06-15-tool-descriptions-verbatim.md) |
| **R16** | 🟩 LOW | Wave-N provenance comments are documentation, not enforcement. Future refactors could silently lose them | all | Add a linter check in CI that flags removed `# LCM Wave-N` comments | [ADR-029](./adr/029-wave-fix-provenance.md) |
| **R17** | 🟩 LOW | OpenClaw `lcm.db` migration via file copy + idempotent re-migrate works only if both schemas are in sync. Schema drift on either side breaks it | 01, 08 | Sample-validate `identity_hash` on N rows after import per [ADR-025](./adr/025-openclaw-migration.md) step 6 | [Spike 003](./spike-results/003-identity-hash.md), [ADR-025](./adr/025-openclaw-migration.md) |
| **R18** | 🟩 LOW | Voyage API rate limits could throttle backfill during initial large-corpus ingestion | 05 | Worker lock TTL = 90s, heartbeat retry budget = 60s, Wave-1 fix backoff cap = 25s, Wave-2 fix Retry-After > 60s immediate-throw | [Spike 004](./spike-results/004-voyage-python-client.md) |
| **R19** | 🟩 LOW | `node:sqlite` is sync, Python `sqlite3` is sync — but mixing sync DB inside async tasks risks transaction-spanning awaits | 02, 04 | LCM already enforces the §0 invariant (no LLM call inside DB tx); preserve it in Python port | [ADR-017](./adr/017-sync-vs-async-db.md) |
| **R20** | 🟩 LOW | `apsw` is non-PEP-249. Swapping driver post-port costs more than isolating it behind `open_db()` factory now | 01 | `open_db()` factory pattern in `db/connection.py` per Epic 01 issue 01-01 + ADR-004 | [ADR-004](./adr/004-sqlite3-backend.md), [Epic 01 issue 01-01](../epics/01-storage/01-01-db-connection.md) |
| **R21** | 🟩 LOW | tier-to-model routing in synthesis is documented in porting-guide synthesis.md but NOT in the TS source. Phase 2 decision needed on whether to enable it for v0.1.0 | 07 | Default to "match TS behavior" (single env var) for v0.1.0; introduce tier ladder in v0.2.0 with eval validation | [Synthesis porting guide](./porting-guides/synthesis.md) §"Tier model", [Epic 07 issue 07-10](../epics/07-entity-synthesis/07-10-tier-routing.md) |
| **R22** | 🟩 LOW | Per-message ingest happens via `post_llm_call` hook diff-on-each-turn; latency vs per-append is higher | 03 | Acceptable for v1; ADR-015 patch #3 (`engine.ingest()` upstream) is the optional cleanup | [ADR-009](./adr/009-per-message-ingest.md) |

## Risk by epic

| Epic | Active risks (severity) |
|---|---|
| Epic 00 | R04 (M), R06 (M), R07 (M) |
| Epic 01 | R17 (L), R20 (L) |
| Epic 02 | R10 (M), R14 (M), R19 (L) |
| Epic 03 | R01 (H), R09 (M), R10 (M), R14 (M), R22 (L) |
| Epic 04 | R11 (M), R14 (M), R19 (L) |
| Epic 05 | R03 (M), R18 (L) |
| Epic 06 | R08 (M), R15 (L) |
| Epic 07 | R11 (M), R21 (L) |
| Epic 08 | R06 (M), R13 (M), R17 (L) |
| Epic 09 | R12 (M) |

## Top 3 to watch

1. **R01 (always-on assembly)** — depends on upstream PR. Start the conversation with Hermes maintainers in Week 1.
2. **R02 (effort estimate)** — re-baseline weekly using actual hours vs issue-level estimates.
3. **R07 (Linux CI)** — easy to verify but not yet done. First PR in Epic 00 must close this.

## Status log

Append a dated line whenever a risk transitions.

- **2026-05-13:** Initial register published. All risks captured from Phase 1 spike + porting work.
