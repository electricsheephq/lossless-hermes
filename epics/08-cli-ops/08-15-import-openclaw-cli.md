---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: lossless-hermes import-openclaw CLI per ADR-025'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: NEW — no TS counterpart (OpenClaw-only migration paths in TS would be unidirectional; this is the Hermes-specific port).
- Lines: ~0 TS (greenfield)
- Specification: ADR-025 §"Decision" lines 71–95.

## Target (Python)

- File: `src/lossless_hermes/cli/import_openclaw.py`
- Estimated LOC: ~280

## What this issue covers

The `lossless-hermes import-openclaw` CLI — one-shot owner-gated migration path from existing OpenClaw `~/.openclaw/` state to Hermes `~/.hermes/lossless-hermes/`. Per ADR-025 §"Decision": explicit CLI (not auto-import, not symlink). Idempotent re-run safe.

### CLI surface (per ADR-025)

```bash
lossless-hermes import-openclaw                          # defaults: from=~/.openclaw, to=~/.hermes/lossless-hermes
lossless-hermes import-openclaw --from /custom/openclaw --to ~/.hermes/profile-b/lossless-hermes
lossless-hermes import-openclaw --force                  # overwrite existing destination
lossless-hermes import-openclaw --validate-rows 1000     # sample 1000 rows for identity_hash check
lossless-hermes import-openclaw --dry-run                # report what would happen; touch nothing
```

Also wired into the `/lcm` slash-command tree as `/lcm import-openclaw` (per ADR-025 line 84) for use within an active Hermes session. The CLI entry point lets operators run it **before Hermes is even started** (no active session needed).

### Algorithm (verbatim from ADR-025 §"Decision" lines 85–94)

1. **Verify source.** Path exists, readable, contains `lcm.db`. Run `PRAGMA integrity_check` against a read-only handle to confirm `lcm.db` is a valid SQLite file.
2. **Verify destination.** Writable. If destination DB exists, refuse unless `--force`. If `--force` is given AND destination has existing data, print the refusal-text-converted-to-warning ("destination has N conversations recorded between $start and $end; --force will discard them").
3. **`shutil.copy2()` `lcm.db`** (not symlink, not move — full copy, preserves timestamps).
4. **`shutil.copytree()` `lcm-files/` → `<dest>/large-files/`.** Skip if source missing.
5. **`shutil.copy2()` `credentials/voyage-api-key`** → `<dest>/credentials/voyage-api-key` if present. Set mode to `0o600`. Destination dir `<dest>/credentials/` created with mode `0o700`.
6. **Open destination DB and run migrations.** `run_lcm_migrations()` is idempotent — preserves `lcm_migration_state` rows per ADR-026.
7. **Sample N rows for identity_hash validation.** Default N=100; configurable via `--validate-rows`. For each, recompute `build_message_identity_hash(role, content)` and compare to stored `identity_hash`. Print summary: `validated=100, matched=98, mismatched=2 (likely pre-existing back-fill drift; not fatal)`. Per ADR-025 line 91 + Spike 003: pre-existing back-fill drift is expected on legacy rows; the mismatched count is non-fatal.
8. **Write `state_meta` row** `lcm_db_imported_at = NOW(), source_path = <source>` so subsequent `import-openclaw` calls fast-fail unless `--force`.
9. **Print operator next steps.** Per ADR-025 line 94:
   ```
   Import complete. Next steps:
     1. Enable lossless-hermes in plugins.enabled in ~/.hermes/config.yaml
     2. Set context.engine: lcm in ~/.hermes/config.yaml
     3. Start Hermes — your conversations are immediately available.
   ```

### Owner-gating

Per ADR-025 §"Consequences" line 108: "Owner-gated: `lossless-hermes import-openclaw` is in the owner-gated CLI surface alongside `/lcm purge`, `/lcm doctor apply`, and `/lcm worker stop`."

The standalone CLI invocation (`lossless-hermes import-openclaw` invoked from a shell) bypasses the gateway gate entirely — single-user CLI invocation is implicitly authorized. The `/lcm import-openclaw` slash-command invocation goes through the same upstream `slash_access.SlashAccessPolicy` as every other destructive command (per ADR-013).

### Disk-usage warning

Per ADR-025 line 113: "Disk usage doubles temporarily. Operators with a 2.6 GB `lcm.db` (Eva's box, per `docs/porting-guides/tests-and-config.md` line 484) need 2.6 GB free at destination. Document in the import command help text."

Print a warning if `shutil.disk_usage(dest).free < shutil.disk_usage(source).total * 1.2` (20% safety margin):

```
WARNING: destination has 1.8 GB free; source is 2.6 GB. Import will likely fail.
Continue anyway? [y/N]
```

`--force` bypasses the interactive prompt; the import proceeds and fails noisily on `OSError: No space left on device` if applicable.

### Schema-version drift handling

Per ADR-025 §"Open questions" #4: v0.1 supports source DBs at LCM v4.1. If the source is older, `run_lcm_migrations()` forward-migrates it (idempotent). If the source is NEWER than the port supports (operator upgraded OpenClaw to a hypothetical v4.2 with new columns), the migration ladder fails on opening; this issue's command surfaces that as a clean error message ("source DB schema is newer than this port supports; upgrade lossless-hermes first").

## Dependencies

- Depends on: #08-01 (dispatcher — for the slash-command alias), Epic 01-01 (`open_lcm_db` factory), Epic 01-04/05/06 (`run_lcm_migrations`), Epic 01-07 (`build_message_identity_hash`), `state_meta` table from Epic 01.
- Blocks: nothing — this is a one-shot operator path.

## Acceptance criteria

- [ ] `lossless-hermes import-openclaw [--from] [--to] [--force] [--validate-rows N] [--dry-run]` argparse surface matches ADR-025.
- [ ] Defaults: `--from=~/.openclaw`, `--to=~/.hermes/lossless-hermes`.
- [ ] Refuses if destination DB exists without `--force`; prints existing-data summary in the refusal message.
- [ ] `--dry-run` reports what would happen and touches nothing.
- [ ] `shutil.copy2()` is used (not move, not symlink, not link) for the DB file.
- [ ] `lcm-files/` → `large-files/` rename per ADR-001/002 layout convention.
- [ ] `voyage-api-key` is copied with `chmod 0o600`; parent dir is `0o700`.
- [ ] `run_lcm_migrations()` runs on the destination DB after copy.
- [ ] Identity-hash sample validation runs against `--validate-rows N` rows (default 100); mismatches are reported but non-fatal.
- [ ] `state_meta.lcm_db_imported_at` is written on success.
- [ ] Disk-space precheck warns when free space < 1.2× source size; `--force` bypasses interactive prompt.
- [ ] Schema-newer-than-supported produces a clean error message.
- [ ] Wired into `/lcm import-openclaw` as a slash-command alias.
- [ ] Standalone CLI invocation bypasses gateway gate (single-user CLI is implicitly authorized).
- [ ] **New test:** `tests/cli/test_import_openclaw.py::test_full_round_trip` (per Epic README "Verification gates" #5) — copy `tests/fixtures/openclaw-mini/lcm.db` (100-conv fixture), assert schema migrated, 100/100 identity-hash sample matched, `state_meta.lcm_db_imported_at` written.
- [ ] **New test:** `tests/cli/test_import_openclaw.py::test_refuse_without_force` — destination DB exists, no `--force` → exit 1 with refusal message.
- [ ] **New test:** `tests/cli/test_import_openclaw.py::test_dry_run_touches_nothing` — `--dry-run` invocation, assert destination directory unchanged.
- [ ] **New test:** `tests/cli/test_import_openclaw.py::test_voyage_api_key_chmod` — credentials copied, `os.stat().st_mode & 0o777 == 0o600`.
- [ ] **New test:** `tests/cli/test_import_openclaw.py::test_idempotent_state_meta` — second call without `--force` exits cleanly; state_meta is not duplicated.
- [ ] Function signatures match ADR-025 §"Decision".
- [ ] `pytest tests/cli/test_import_openclaw.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/cli/import_openclaw.py`).
- [ ] PR description cites ADR-025 (and LCM commit `1f07fbd` for the schema/identity-hash invariants).

## Estimated effort

**6 hours.**

## Confidence

**90%** — fully specified in ADR-025; Spike 003 confirms identity_hash byte-parity; `shutil.copy2` + `run_lcm_migrations()` are straightforward primitives. The 10% risk is in the `state_meta` row insert idempotency on `--force` (re-running force should rewrite, not duplicate), validated by a dedicated test.
