# Epic 01 — Storage layer

**Status: closed** — all 15 issues merged (PRs #10–#22, #24; 01-11's FTS5 helpers shipped with the SummaryStore in #22); v0.1.0 release gate.

## Goal

Port LCM's entire storage layer (DB connection, 27 tables, 42 indexes, 13 stores, identity hash, large-files, integrity, prune, transcript-repair, transaction-mutex) from TypeScript (`lossless-claw` @ `pr-613`, commit `1f07fbd`) to Python under `src/lossless_hermes/`. Storage is the substrate every other epic builds on — get the schema, idempotency, and dedup invariants right here or pay for it everywhere downstream.

## Deliverables

- `src/lossless_hermes/db/` — `connection.py`, `config.py`, `features.py`, `migration.py` (the 2,037-LOC schema ratchet).
- `src/lossless_hermes/store/` — 11 modules: `conversation.py`, `summary.py`, `compaction_telemetry.py`, `compaction_maintenance.py`, plus 7 support helpers (`message_identity`, `parse_utc_timestamp`, `conversation_scope`, `fts5_sanitize`, `full_text_sort`, `full_text_fallback`, `__init__.py` barrel).
- `src/lossless_hermes/transaction_mutex.py` — per-DB reentrant lock + savepoint-based nested transactions.
- `src/lossless_hermes/large_files.py` — `<file>` block parsing + deterministic exploration summaries (567 LOC source).
- `src/lossless_hermes/integrity.py` — 8 checks + repair plan (600 LOC).
- `src/lossless_hermes/prune.py` — age-based pruning with dry-run / VACUUM modes (392 LOC).
- `src/lossless_hermes/transcript_repair.py` — tool_use ↔ tool_result pairing repair (300 LOC, **no JSONL-rewrite path** — that lives in engine.ts).
- **Fully idempotent migrations** — `run_lcm_migrations(conn)` is a no-op on already-migrated DBs (per ADR-026). 3 versioned backfills tracked in `lcm_migration_state`.
- **200+ ported pytest cases** covering identity-hash byte-parity (spike 003 fixture), migration end-to-end, FTS5 + trigram, transaction mutex stress, prune cascade, integrity, large-files.

## Dependencies

- **Depends on:** Epic 00 (scaffolding — `pyproject.toml`, `pytest.ini`, package layout, CI matrix).
- **Pins:** `sqlite-vec==0.1.9`, optional `apsw==3.53.1.0` extra (per ADR-004).

## Blocks

- **Epic 02** (engine skeleton) — needs `db/connection.py`, `store/conversation.py`, `store/summary.py`, and the migration ladder to instantiate `LcmContextEngine`.
- **Epic 03** (ingest assembly) — needs message-part stores + identity hash.
- **Epic 04** (compaction) — needs `summary` store + `compaction_telemetry` / `compaction_maintenance`.
- **Epic 05** (embeddings) — `lcm_embedding_profile` and `lcm_embedding_meta` tables are created here; the vec0 virtual table itself is created in Epic 05 but its sidecar tables and the polymorphic FK trigger (`lcm_embedding_meta_cleanup_summary`) belong to this epic.
- **Epic 07** (entity synthesis) — `lcm_entity_*` and `lcm_prompt_registry` / `lcm_synthesis_cache` / `lcm_synthesis_audit` / `lcm_cache_leaf_refs` tables are all created here; Epic 07 wires the seeding callback (ADR-005 option A).

## Critical path

**YES.** Nothing else in the project can be tested end-to-end without the schema and connection layer landing first. Epic 00 → Epic 01 → everything else.

## Estimated total effort

**3–4 weeks (~120–180 hours)** of focused engineering. Breakdown from `docs/porting-guides/storage.md` §1 (8,392 TS LOC → ~9,500 Python LOC including test bench):

- DDL + migration ladder: ~30–40 h (largest single chunk — `migration.ts` is 2,037 LOC).
- Conversation + Summary stores: ~40–50 h combined.
- Helpers, large-files, integrity, prune, mutex, transcript-repair: ~30–40 h.
- Test-port + parity work: ~25–35 h.
- ADR/spike follow-through, cross-store integration smoke: ~10 h.

## Confidence

**95% (spike-verified).**

- Spike 001 PASS — stdlib `sqlite3` loads `sqlite-vec`, hosts vec0 + FTS5 + trigram on one connection (Homebrew Python 3.12 / 3.14).
- Spike 003 PASS — `identity_hash` byte-identical between Node, Python, and Go on 10-case fixture (ASCII, CJK, emoji ZWJ families, embedded NUL, 8 KiB content, etc.).
- Spike 005 PASS — Python stdlib `sqlite3` has FTS5 + porter + trigram + bm25 + snippet + UNINDEXED across every tested interpreter (SQLite 3.51–3.53).

Residual 5% risk lives in concurrency stress, `PRAGMA optimize`-on-close behavior, Linux CI matrix coverage, and the `AsyncLocalStorage` → `contextvars` translation for the transaction-mutex savepoint depth tracker — all called out in `docs/porting-guides/storage.md` §12.

## Issues

15 issues, port-order-aware (pure-function leaves first, then connection, then schema, then stores, then top-level utilities):

| # | Title | Hours | Confidence |
|---|---|---:|---:|
| 01-01 | DB connection + PRAGMAs + extension flag | 4–6 | 95% |
| 01-02 | DB config — pydantic model + 67-var env resolver | 8–12 | 95% |
| 01-03 | DB features — FTS5 + trigram probes | 2 | 98% |
| 01-04 | Migration: 11 core tables + 42 indexes (largest issue) | 30–40 | 90% |
| 01-05 | Migration: 3 FTS5 virtual tables (verify spike 005) | 4–6 | 95% |
| 01-06 | Migration: 13 v4.1 tables (worker_lock, queue, audit, prompts, synthesis, eval, entities, embeddings, feature_flags) | 16–22 | 92% |
| 01-07 | Message identity hash (spike 003 fixture: 10 byte-parity cases) | 1 | 98% |
| 01-08 | ConversationStore (1,219 LOC) | 18–22 | 92% |
| 01-09 | SummaryStore (1,569 LOC) | 22–28 | 92% |
| 01-10 | Telemetry + maintenance stores | 4 | 95% |
| 01-11 | FTS5 helpers — sanitize, sort, fallback, parse-utc, scope | 4 | 95% |
| 01-12 | Large-files (567 LOC) — block parse, MIME map, file-id, exploration summaries | 8 | 92% |
| 01-13 | Integrity + Prune + Transaction-mutex | 13–18 | 90% |
| 01-14 | Transcript-repair (319 LOC, no JSONL-rewrite) | 5 | 95% |
| 01-15 | Versioned backfills (depths, metadata, tool_call_id) | 6–8 | 92% |

Approximate total: **145–197 hours** — congruent with the 120–180 h estimate after accounting for ADR/integration overhead.

## ADRs that gate this epic

All accepted at 95%+:

- **ADR-003** (database isolation) — own `lcm.db` file at `$HERMES_HOME/lossless-hermes/lcm.db`.
- **ADR-004** (sqlite3 backend) — stdlib `sqlite3` primary, `apsw` opt-in extra.
- **ADR-017** (sync vs async DB) — synchronous end-to-end; async only at HTTP boundary.
- **ADR-024** (project layout) — 1:1 mirror under `src/lossless_hermes/`.
- **ADR-026** (schema versioning) — keep LCM's two-mechanism design (structural probes + `lcm_migration_state` for versioned backfills); no monotonic `schema_version`.

## Out of scope for this epic

- **vec0 virtual table creation** — `lcm_embeddings_<model>` lives in Epic 05; this epic only creates the sidecar `lcm_embedding_profile` + `lcm_embedding_meta` tables and the polymorphic-cleanup trigger.
- **Default synthesis prompt seeding** — `seedDefaultPrompts(conn)` content moves to Epic 07; migration accepts a `seed_default_prompts: Callable | None = None` parameter and skips when `None` (ADR-005 option A).
- **PR #628 stub-tier externalization** — script (`scripts/lcm-blob-migrate.mjs`) + `is_externalized` column on `message_parts` is a separate post-pr-613 epic (per ADR-030).
- **JSONL bootstrap, file-anchor checkpointing, session-file rollover** — Hermes is SQLite-only; the ~1,800 LOC of `engine.ts` JSONL paths drops entirely.

## Verification gates before close

- [x] 1. `pytest tests/` green on `macos-latest` (Homebrew Python 3.12) + `ubuntu-latest` (3.13) + `python:3.11-slim` Docker (per ADR-004 open-questions §1–2). — green on all 6 CI matrix cells; Wave 2 closed with 907 tests passing.
- [x] 2. `pytest tests/test_message_identity.py` passes the 10-case spike-003 fixture. — 01-07 (#10) ported the byte-identical SHA-256 hash + 10-case parity fixture.
- [x] 3. Schema-diff: Python-generated schema vs. TS-generated reference DB has zero diff outside expected formatting (per `docs/reference/lcm-source-map.md` open-question #2). — `scripts/schema_diff.sh --verify-subset` CI GREEN, 92/92 schema objects matched.
- [x] 4. `run_lcm_migrations()` twice in a row is a no-op on the second call (idempotency invariant per ADR-026). — 01-04 (#15), 01-05 (#16), 01-06 (#20) migration ladder; 01-15 (#24) versioned backfills close the idempotency invariant.
- [x] 5. `mypy src/lossless_hermes/db src/lossless_hermes/store` passes strict. — `ty check` green across the DB + store modules on all CI cells.
- [x] 6. A v4.1 OpenClaw `lcm.db` copied into `$HERMES_HOME/lossless-hermes/lcm.db` runs migrations cleanly and reports 0 backfill rows newly written (everything already at algorithm_version 1). — 01-15 (#24) versioned backfills (depths, metadata, tool_call_id) verified no-op on already-migrated DBs.
