# ADR-025: Migration from existing OpenClaw LCM users

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 90%
**Supersedes:** —
**Superseded by:** —

## Context

Existing operators run `lossless-claw` as an OpenClaw plugin, with state under `~/.openclaw/`:

- `~/.openclaw/lcm.db` — the LCM SQLite database (conversations, messages, summaries, context_items, embeddings vec0 virtual tables, prompt registry, eval rows, etc.)
- `~/.openclaw/lcm-files/` — externalized large-file blobs referenced by `messages.content`
- `~/.openclaw/credentials/voyage-api-key` — Voyage API key (used for embedding-backfill)

`lossless-hermes` writes to a separate root (`~/.hermes/lossless-hermes/` per ADR-001 + the YAML config option in `tests-and-config.md` §"Hermes config delivery"). Operators migrating from OpenClaw to Hermes need a one-shot path that does not require re-ingesting their conversation history — the existing `lcm.db` is fully compatible with the Python port because:

- **Storage layer is byte-compatible.** `docs/porting-guides/storage.md` §"10.2 Migration from existing OpenClaw lcm.db" — the TS migration is fully idempotent; running the Python `run_lcm_migrations()` against an OpenClaw-populated DB is a no-op for present columns/tables/indexes.
- **`identity_hash` round-trips byte-identical.** Spike 003 (`docs/spike-results/003-identity-hash.md`) confirms Node, Python, and Go produce identical SHA-256 digests across 10 cases including CJK, ZWJ emoji, embedded NUL, JSON-stringified arrays. Dedup-on-replay is safe.
- **`lcm_migration_state` rows survive.** The two-column versioned-backfill ledger (ADR-026) is preserved; backfills already at `algorithm_version >= 1` are skipped.

The constraint forcing a choice: how do operators trigger this — automatic on first start, manual CLI, or operator-supplied flag?

## Options considered

### Option A: Explicit CLI command `lossless-hermes import-openclaw`

- Description: One-shot owner-gated CLI command. Default expects `~/.openclaw`; accepts `--from PATH` and `--to PATH` for nonstandard locations. Steps:
  1. Verify the source path exists and contains `lcm.db`.
  2. Refuse if destination `lossless-hermes/lcm.db` already exists (require `--force`).
  3. `cp openclaw/lcm.db hermes/lossless-hermes/lcm.db` (full file copy, not link, so the OpenClaw install keeps working).
  4. `cp -r openclaw/lcm-files/* hermes/lossless-hermes/large-files/` (preserves blob references).
  5. `cp openclaw/credentials/voyage-api-key hermes/lossless-hermes/credentials/` if present.
  6. Open the copied DB and run `run_lcm_migrations()` (idempotent — no-op on already-migrated rows).
  7. Sample N random rows (default N=100), recompute `identity_hash(role, content)` with the Python hasher, assert byte-identical to stored value. Report mismatch as a non-fatal warning with row count.
  8. Write `state_meta` row `lcm_db_imported_at = NOW()` so subsequent launches skip the import check (`docs/porting-guides/storage.md` line 577).
- Pros:
  - Operator action is explicit. No surprise on first-run that touches a separate tree.
  - Owner-gated (creates files; touches credentials) — fits the operator-CLI surface the rest of the `/lcm` command tree uses.
  - Idempotency is the default; re-running is safe.
  - Sampling step (#7) catches pre-existing drift (Eva's known case of legacy rows back-filled before `identity_hash` existed — `src/db/migration.ts:326–344`, called out in spike 003 "Remaining 5% risk").
- Cons:
  - Requires operator to read the README and run a command. First-time onboarding has one extra step.
- Evidence cited:
  - `docs/porting-guides/storage.md` §10 "Migration from existing OpenClaw lcm.db" (lines 572–577) — exact step sequence + idempotent migration ladder.
  - `docs/spike-results/003-identity-hash.md` §"Recommendation" (line 290) — pin identity_hash with fixture test; sample N rows during import.
  - `docs/porting-guides/storage.md` §10.3 — vec0 virtual tables survive file copy.

### Option B: Auto-import on first start when `~/.openclaw/lcm.db` is detected

- Description: At `register(ctx)` time, if `~/.hermes/lossless-hermes/lcm.db` does not exist AND `~/.openclaw/lcm.db` does, perform the import automatically with no operator prompt.
- Pros:
  - Zero-config migration. Operators "just upgrade" and it works.
- Cons:
  - Touches the operator's filesystem implicitly. Violates the principle that file-creating actions are owner-gated.
  - Edge cases: what if the operator deliberately wants a fresh Hermes start? What if they have multiple OpenClaw profiles? Auto-detect makes one choice for them.
  - Migration failures during plugin init are hard to surface — Hermes silently skips broken plugins (`hermes_cli/plugins.py:1218–1232`, see ADR-001 "Open questions" #3).
  - Doubles the cost of `register(ctx)` time on first launch (file copy + DB open + sample-validation) — a startup-latency regression for users who never had OpenClaw.

### Option C: Symlink instead of copy

- Description: `ln -s ~/.openclaw/lcm.db ~/.hermes/lossless-hermes/lcm.db`. Reuse the same physical DB file.
- Pros: No disk-space duplication.
- Cons:
  - Both LCM and Hermes write to the same DB concurrently if the operator keeps both installs active. SQLite handles this with WAL mode, but cross-installation drift (`lcm_migration_state` advancing on one side and not the other) is a foot-gun.
  - Backups become entangled: Hermes-side `db_backup.py` snapshots affect OpenClaw too.
  - Operators who want to roll back to OpenClaw later cannot diverge.

## Decision

Chosen: **Option A — explicit owner-gated `lossless-hermes import-openclaw` CLI**.

CLI invocation:

```bash
$ lossless-hermes import-openclaw                  # defaults: from=~/.openclaw, to=~/.hermes/lossless-hermes
$ lossless-hermes import-openclaw --from /custom/openclaw --to ~/.hermes/profile-b/lossless-hermes
$ lossless-hermes import-openclaw --force          # overwrite existing destination
$ lossless-hermes import-openclaw --validate-rows 1000  # sample 1000 rows for identity_hash check
$ lossless-hermes import-openclaw --dry-run        # report what would happen; touch nothing
```

The command is owner-gated (file-creating; touches credentials). It is wired through the `/lcm` slash-command tree as `/lcm import-openclaw` and also exposed as a top-level CLI subcommand for use before Hermes is even running (no active session needed). Step-by-step behavior:

1. Verify source path exists and is readable; verify `<source>/lcm.db` is a valid SQLite file (run `PRAGMA integrity_check` against a read-only handle).
2. Verify destination is writable. If destination DB exists, refuse unless `--force`.
3. `shutil.copy2()` `lcm.db` (not symlink, not move — full copy, preserves timestamps).
4. `shutil.copytree()` `lcm-files/` → `<dest>/large-files/`. Skip if source missing.
5. `shutil.copy2()` `credentials/voyage-api-key` → `<dest>/credentials/voyage-api-key` if present. Set mode to 0o600.
6. Open the destination DB; call `run_lcm_migrations()` (idempotent; preserves `lcm_migration_state` rows per ADR-026).
7. Sample N rows from `messages` (default N=100; configurable via `--validate-rows`). For each, recompute `build_message_identity_hash(role, content)` and compare to stored `identity_hash`. Print a summary: `validated=100, matched=98, mismatched=2 (likely pre-existing back-fill drift; not fatal)`.
8. Insert `state_meta` row `lcm_db_imported_at = NOW(), source_path = <source>` so subsequent `import-openclaw` calls fast-fail unless `--force`.
9. Print operator next steps: enable `lossless-hermes` in `plugins.enabled`; set `context.engine: lcm` in `config.yaml` (per ADR-001 consequences).

## Rationale

Spike 003 (`docs/spike-results/003-identity-hash.md`) confirms `identity_hash` is byte-identical across Node, Python, and Go on 10 test fixtures spanning ASCII, CJK, ZWJ emoji, embedded NUL, JSON-stringified arrays, 8 KiB content, and an empty boundary. The migration's correctness contract — "the same `(role, content)` tuple produces the same hash; pre-existing rows do not need re-hashing" — is empirically established.

Storage-porting-guide §10 (`docs/porting-guides/storage.md` lines 569–595) documents the exact step sequence and confirms the migration ladder is fully idempotent: `runLcmMigrations()` on an existing DB is a no-op for present columns/tables/indexes; `lcm_migration_state` ensures versioned backfills already at `algorithm_version >= 1` are skipped (ADR-026 keeps this contract).

Auto-import (Option B) was rejected because file-creating operations belong on an explicit operator path. The cost of one extra CLI command is small; the cost of mis-migrating a multi-gigabyte production DB on plugin init is large.

Symlink (Option C) was rejected because it entangles the two installs in ways that break the rollback story (operators who try Hermes and want to go back).

## Consequences

- **Owner-gating:** `lossless-hermes import-openclaw` is in the owner-gated CLI surface alongside `/lcm purge`, `/lcm doctor apply`, and `/lcm worker stop` (the file-creating / credential-touching commands).
- **Idempotent re-run:** running `import-openclaw` twice without `--force` is a no-op (step 2 refuses). Running with `--force` overwrites destination — operator-acknowledged destructive action.
- **Drift detection:** the sample-validation step (#7) makes pre-existing back-fill drift surface immediately rather than during the first real assemble call. Drift count is reported, not auto-repaired — operators can re-run `/lcm doctor apply` afterwards if needed.
- **Embeddings survive:** vec0 virtual tables (`lcm_embeddings_<model>`) live inside `lcm.db` and survive the file copy. If sqlite-vec is not loadable in the destination Python env, semantic-search queries error at runtime (documented in `docs/porting-guides/storage.md` §10.3); semantic-retrieval feature-flags to disabled with a one-time warning.
- **Credentials:** `voyage-api-key` is copied with mode 0o600. The destination directory `<dest>/credentials/` is created with mode 0o700.
- **Disk usage doubles temporarily.** Operators with a 2.6 GB `lcm.db` (Eva's box, per `docs/porting-guides/tests-and-config.md` line 484) need 2.6 GB free at destination. Document in the import command help text.
- **OpenClaw install keeps working.** Source files are unchanged; operators can run both stacks in parallel during evaluation.
- **Invariant:** the Python `run_lcm_migrations()` MUST be byte-compatible with the TS migration ladder on already-present columns/tables/indexes. Validation: schema-diff script in CI (per `docs/reference/lcm-source-map.md` "Open questions" #2 — diff Python-generated schema against a TS-generated reference DB).

## Open questions / 5% uncertainty

1. **Operators with a single mega-`lcm.db` (>10 GB).** The full file copy step is O(size). For Eva's 2.6 GB DB this is ~30 seconds on SSD; for larger DBs it could be minutes. Mitigation: print a progress indicator; consider `--link` flag (hard link, same filesystem only) as a fast path. Not implemented in v0.1.
2. **Multiple OpenClaw profiles.** If an operator has `~/.openclaw-test/` and `~/.openclaw-prod/`, they need to invoke with `--from` twice and `--to` twice (one per Hermes profile). The CLI supports this; documentation must spell it out.
3. **Hermes-side prior state.** If the operator has already created Hermes-side LCM data (e.g. ran `lossless-hermes` for a week before discovering they had old OpenClaw data), `--force` overwrites and loses the new data. Mitigation: the refusal step (#2) prints "destination has N conversations recorded between $start and $end; --force will discard them". Operator-acknowledged.
4. **Schema-version drift.** If the source OpenClaw `lcm.db` was created by an OLDER LCM than the Python port supports (e.g. v3.x without the v4.1 `summaries.source_message_token_count` column), the migration ladder forward-migrates it. If it was created by a NEWER LCM than the port supports (operator upgraded OpenClaw to a hypothetical v4.2 with new columns), the Python port might fail to open it. Mitigation: ADR-026 keeps the `lcm_migration_state` ledger, which makes downgrades detectable. v0.1 supports source DBs at LCM v4.1; later mismatch is documented as "upgrade Hermes first".
