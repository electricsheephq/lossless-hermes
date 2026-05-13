# ADR-003: Database isolation

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

Hermes maintains its own SQLite database at `$HERMES_HOME/state.db` (used by `hermes_state.py` for FTS5 conversation indexing, session state, message logs — see `/Volumes/LEXAR/Claude/hermes-agent/hermes_state.py:253-306`). Lossless-hermes has a substantially larger and stricter schema:

- LCM v4.1 carries **27 tables and 42 indexes** (per the storage analysis in the v4 codebase).
- A multi-step migration ladder (per-version SQL files applied in order via `pragma user_version`).
- `vec0` virtual tables (KNN embeddings) and FTS5 + trigram tables co-located in the same file (spike 001 §"FTS5 trigram tokenizer co-existence").
- Periodic VACUUM + integrity-check passes.

We must decide whether LCM's tables live in:

1. **Hermes's existing `state.db`** (one file, multiple owners), OR
2. **An LCM-owned database** at `$HERMES_HOME/lossless-hermes/lcm.db`.

## Options considered

### Option A: Own database at `$HERMES_HOME/lossless-hermes/lcm.db`

- Description: open a separate SQLite file; all 27 LCM tables and 42 indexes live there. Hermes never sees them.
- Pros:
  - **Migration ladder isolation.** LCM's `user_version` and Hermes's `user_version` are separate counters in separate files. Bumping one does not affect the other. Cross-plugin migration races are structurally impossible.
  - **Safe OpenClaw migration.** Existing OpenClaw users can `cp ~/.openclaw/lcm.db $HERMES_HOME/lossless-hermes/lcm.db` and the schema lands intact — no need to rewrite tables, rebuild FTS5, or reconcile `user_version` against Hermes's counter. This is the canonical migration path documented in ADR-002.
  - **No cross-DB JOIN need.** LCM's read path is `vec0` MATCH → `messages` table → `summaries` table — all internal. We do not JOIN against Hermes's `conversations` or `messages` tables. The integration points (`pre_llm_call`, `post_llm_call`) hand us message dicts; we don't read Hermes's SQLite.
  - **Backup, audit, and inspection are scoped.** `sqlite3 $HERMES_HOME/lossless-hermes/lcm.db .schema` shows only LCM tables. Backups in `$HERMES_HOME/lossless-hermes/backups/` are pure LCM state.
  - **Schema namespace is clean.** No risk of name collision between an LCM table (`messages`, `summaries`, `conversations`) and a Hermes table of the same name.
  - **Concurrency model is owned.** LCM picks its own `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `cache_size=-64000` settings without conflicting with Hermes's choices.
  - **Hermes upstream churn doesn't affect us.** If Hermes upstream adds, renames, or rebuilds a `state.db` table, LCM is unaffected.
- Cons:
  - Two open connections per process. Memory cost: ~negligible (a few MB of page cache per connection).
  - Operators see two DB files in `$HERMES_HOME`; mental model slightly heavier.
- Evidence cited:
  - 27 tables + 42 indexes count: documented in the LCM v4.1 source storage layer (per the porting brief).
  - vec0 + FTS5 co-existence on a single connection: spike 001 §"FTS5 trigram tokenizer co-existence".
  - OpenClaw `~/.openclaw/lcm.db` file precedent: spike 001 §Findings, ADR-002 §Rationale, `dependencies.md` line 170.
  - No JOIN-against-Hermes need: `hermes-hooks.md` §"Where LCM hooks land" — every integration is via dicts passed through hooks, not via shared SQLite.

### Option B: Share Hermes's `state.db`

- Description: open `$HERMES_HOME/state.db`, run LCM migrations on top of Hermes's tables.
- Pros:
  - One file to back up / inspect.
  - Single connection per process if LCM and Hermes share it (saves a few MB of page cache).
- Cons:
  - **Migration ladder collision.** Both Hermes and LCM use `pragma user_version` as their migration counter. There is only one counter per database. If Hermes upstream bumps `user_version` from 14 to 15 (their version of "we added column X"), LCM's migration ladder (which thinks it's still at "LCM version 7") will not run on the next startup because LCM expects a different counter semantics.
  - **OpenClaw migration becomes a port, not a copy.** Existing users cannot `cp ~/.openclaw/lcm.db` into `state.db` — the tables would need to be SELECT/INSERT'ed table-by-table, the FTS5 indexes rebuilt, and the vec0 vectors re-serialized. Hours of porting work.
  - **Table-name collision risk.** Hermes already has `conversations`, `messages`, `sessions` tables in `state.db` (`hermes_state.py:253-306`). LCM also has `conversations`, `messages`, `summaries`. We would need a prefix (`lcm_conversations`, `lcm_messages`) — but the OpenClaw codebase doesn't have that prefix, so every query string changes.
  - **Concurrency contention.** Hermes writes to `state.db` from its own threads; LCM writes from its embedding workers and from `post_llm_call`. Both contending on the same WAL file means `SQLITE_BUSY` becomes more frequent.
  - **Upstream churn risk.** A future Hermes release that adds, renames, or `VACUUM`s a `state.db` table can break LCM's reads. We become tightly coupled to Hermes's internal schema.
  - **No clean uninstall.** `pip uninstall lossless-hermes` leaves LCM's tables stranded in Hermes's `state.db`. A dedicated DB file can be deleted; embedded tables cannot.
- Evidence cited:
  - Hermes's existing schema lives in `/Volumes/LEXAR/Claude/hermes-agent/hermes_state.py:253-306` — confirms tables named `messages`, `conversations` already exist.
  - Spike 002 confirmed Hermes hooks pass full message-dict snapshots — there is no implicit need to share storage.

### Option C: Hybrid — LCM owns its file, but stores a foreign-key handle to Hermes's `sessions` table for cross-reference

- Description: own `lcm.db` but write a `hermes_session_id` column on the LCM `conversations` table for cross-correlation.
- Pros: own DB benefits (per Option A) plus cross-reference capability.
- Cons:
  - The cross-correlation is non-load-bearing for v0.1 — LCM uses session_id as a string key, not a foreign-key constraint.
  - Foreign-keys-across-files require ATTACH; ATTACH locks both files and complicates the connection-open path.
  - Adds engineering surface for a feature we don't need.
- Evidence: same as Option A; the cross-reference column can be added later without schema breakage.

## Decision

Chosen: **Option A — own database at `$HERMES_HOME/lossless-hermes/lcm.db`**.

## Rationale

The dominant constraint is OpenClaw migration: existing users have `~/.openclaw/lcm.db` files that must port over without rewriting tables. Option A makes this a literal `cp` operation; Option B turns it into a per-table SELECT/INSERT/REBUILD-FTS5 dance that takes hours and is failure-prone.

The second constraint is migration-ladder isolation. SQLite's `pragma user_version` is a single int32 per file; you cannot have two independent migration counters in the same DB without ad-hoc bookkeeping. Hermes already uses it (`hermes_state.py:264-275`), and LCM uses it (the migration ladder is documented in `lcm-source-map.md`). Two owners, one counter, one file means upstream Hermes can silently break our migration runner.

There is no JOIN need that would justify shared storage. LCM's integration with Hermes is via in-memory message dicts handed through hooks (`hermes-hooks.md` §"Where LCM hooks land"), not via shared SQLite reads. The two stores have no overlapping query path.

Vec0 + FTS5 co-existence is already proven on a single connection (spike 001 §"FTS5 trigram tokenizer co-existence"), so we don't need to split LCM further — one LCM-owned file holds all 27 LCM tables, the FTS5 + trigram tables, and the vec0 tables.

The Option C hybrid was rejected because the cross-reference column it adds is non-load-bearing and ATTACH-based foreign keys are operationally costly.

## Consequences

- **Connection-open factory required.** `lossless_hermes.db.open_lcm_db(path)` is the only sanctioned way to open the database; it loads `sqlite-vec`, sets PRAGMA tunings, and applies migrations. Mirrors the spike-001 pattern.
- **Migration ladder is owned end-to-end.** `pragma user_version` on `lcm.db` is LCM's; we never read or write the corresponding counter in Hermes's `state.db`.
- **Hermes upstream can change `state.db` freely.** We have zero dependency on its schema.
- **One file to back up.** Backup rotation in `$HERMES_HOME/lossless-hermes/backups/` snapshots only `lcm.db`. Hermes's backup story is independent.
- **OpenClaw migration is `cp`.** `hermes lcm migrate-from-openclaw` copies `~/.openclaw/lcm.db → $HERMES_HOME/lossless-hermes/lcm.db` and runs `pragma user_version` to validate.
- **Precluded:** No `ATTACH 'state.db' AS hermes` calls. If we ever need cross-store data, we read from the in-process Python dicts passed through Hermes hooks — not from Hermes's SQLite directly.
- **Invariant:** the entire LCM schema lives in `lcm.db` and nowhere else. No spillover tables in `state.db`. No alternate-file tables.

## Open questions / 5% uncertainty

1. **Multi-process write contention on `lcm.db`.** If a future Hermes deployment spawns multiple agent processes against one `$HERMES_HOME`, all of them open `lcm.db` simultaneously. Spike 001 §"Remaining 5% risk" item 4 notes that multi-connection WAL semantics with vec0 are documented as supported but not exercised. Mitigation: a follow-up multi-process write spike before claiming production-grade for high-concurrency deployments.
2. **Disk-quota exhaustion.** If `lcm.db` fills the disk on a host where Hermes's `state.db` is small, both stores fail. Mitigation: expose `plugins.entries.lossless-hermes.storage.max_db_size_mb` and refuse writes above the cap (degrading gracefully to read-only).
3. **`VACUUM` lock duration.** LCM's `VACUUM` pass locks the whole `lcm.db` for the duration. On a multi-GB DB this can be tens of seconds. Mitigation: schedule VACUUM on a background timer, never on the hot ingest path. Document the lock window.
4. **Migration tool overwrites.** `hermes lcm migrate-from-openclaw` must refuse to overwrite an existing `lcm.db` unless `--force` is passed. Otherwise an operator who runs migration twice loses recent writes.
5. **Cross-DB analytics queries.** If we later want to JOIN LCM `summaries` against Hermes `sessions` for analytics, we either ATTACH at query time (cheap but file-system-dependent) or pull via two queries and join in Python. Defer the decision until the use case is concrete.