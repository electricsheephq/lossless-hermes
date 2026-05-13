# ADR-002: Plugin data directory layout

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

Lossless-hermes is a persistent context engine: it owns a SQLite database (`lcm.db`), credential files, large-file blobs split out from message payloads, and periodic backups. We need a single canonical filesystem layout under `$HERMES_HOME` that:

- Co-locates LCM state with other Hermes plugin state (operators expect plugin data to live under `$HERMES_HOME`, not in arbitrary `~/.config/lossless-hermes/` style paths).
- Enables a one-command migration for existing OpenClaw users (`~/.openclaw/lcm.db`, `~/.openclaw/lcm-files/`, `~/.openclaw/credentials/voyage-api-key`) — they should be able to `cp` files across without renaming.
- Plays nicely with Hermes's profile system (`get_hermes_home()` may resolve to `~/.hermes` or to a per-profile root — see `hermes_constants.py:14-106`).
- Avoids stomping on Hermes's own state (`$HERMES_HOME/state.db`, `$HERMES_HOME/config.yaml`, `$HERMES_HOME/logs/`).

## Options considered

### Option A: `$HERMES_HOME/lossless-hermes/` with subdirectories

- Description: own a single top-level directory; everything LCM lives under it.
  ```
  $HERMES_HOME/lossless-hermes/
    lcm.db                            # SQLite (own database — see ADR-003)
    config.yaml                       # operator knobs (optional override of Hermes config)
    credentials/
      voyage-api-key                  # 0600 file, primary file-source for VOYAGE_API_KEY
    large-files/
      <conv_id>/<sha256>.bin          # spilled large message payloads
    backups/
      lcm-<iso8601>.db                # rotating backups
  ```
- Pros:
  - Mirrors the precedent set by Hermes's own plugins: hindsight uses `$HERMES_HOME/hindsight/config.json` and `$HERMES_HOME/hindsight/` subdirs (`/Volumes/LEXAR/Claude/hermes-agent/plugins/memory/hindsight/__init__.py:305,615`).
  - Migration from OpenClaw is a literal file copy:
    ```
    cp ~/.openclaw/lcm.db                              $HERMES_HOME/lossless-hermes/lcm.db
    cp -r ~/.openclaw/lcm-files/                       $HERMES_HOME/lossless-hermes/large-files/
    cp ~/.openclaw/credentials/voyage-api-key          $HERMES_HOME/lossless-hermes/credentials/voyage-api-key
    ```
    No path-rewriting, no schema munging.
  - Profile-aware: `get_hermes_home()` resolves to the active profile root, so per-profile LCM state "just works" (`hermes_constants.py:88-106`).
  - Self-contained: one directory to back up, audit, or delete.
- Cons:
  - Directory name `lossless-hermes` (12 chars + hyphen) is longer than OpenClaw's flat `~/.openclaw/`. Operators typing paths interactively pay a small cost.
- Evidence cited:
  - `hermes-hooks.md` line 326 — `ContextEngine.on_session_start` receives `hermes_home` in kwargs, making `get_hermes_home() / "lossless-hermes"` the canonical resolution.
  - Hindsight precedent: `/Volumes/LEXAR/Claude/hermes-agent/plugins/memory/hindsight/__init__.py:298-305` (`get_hermes_home() / "hindsight" / "config.json"`).
  - OpenClaw layout: `dependencies.md:170` cites `~/.openclaw/credentials/voyage-api-key`.

### Option B: Flat files at `$HERMES_HOME/` root (mirror Honcho)

- Description: drop `lossless-hermes.db`, `lossless-hermes-config.yaml`, etc. directly at the `$HERMES_HOME` root.
- Pros:
  - Matches `honcho` precedent — `$HERMES_HOME/honcho.json` is a single root-level file (`/Volumes/LEXAR/Claude/hermes-agent/plugins/memory/honcho/client.py:71`).
  - One fewer directory in the path.
- Cons:
  - LCM has at least four classes of state (DB, credentials, large-file blobs, backups). Flat root-level files don't scale past one or two artifacts.
  - Honcho gets away with this because Honcho's local state is one JSON config file — the heavy lifting lives in the remote Honcho service. LCM has substantial local state.
  - Migration from OpenClaw becomes a per-file rename:
    ```
    cp ~/.openclaw/lcm.db                              $HERMES_HOME/lossless-hermes.db
    cp ~/.openclaw/lcm-files/                          $HERMES_HOME/lossless-hermes-files/   # rename
    ```
  - Pollutes the `$HERMES_HOME` root with plugin-specific files; harder to audit "what does LCM own."
- Evidence cited:
  - Honcho's flat layout: `/Volumes/LEXAR/Claude/hermes-agent/plugins/memory/honcho/client.py:71`.
  - OpenClaw layout for migration delta: `dependencies.md:170`.

### Option C: `$HERMES_HOME/lossless/` (shorter prefix, matches dependencies.md)

- Description: use `lossless` (no `-hermes` suffix) as the directory name.
- Pros:
  - `docs/reference/dependencies.md:164-170` already references `$HERMES_HOME/lossless/credentials/voyage-api-key` in the credential-resolution section.
  - Shorter than `lossless-hermes/`.
- Cons:
  - `lossless` alone is ambiguous in `$HERMES_HOME` listings — an operator scanning `ls ~/.hermes/` doesn't immediately see "that's the lossless-hermes plugin's directory."
  - Splits the namespace: package name is `lossless-hermes`, entry-point name is `lossless-hermes`, but the data dir is `lossless`. Three names for one thing.
- Evidence cited:
  - `dependencies.md:164-170` uses `$HERMES_HOME/lossless/`.

## Decision

Chosen: **Option A — `$HERMES_HOME/lossless-hermes/` with subdirectories** (`lcm.db`, `credentials/`, `large-files/<conv_id>/`, `backups/`).

## Rationale

Hermes's own precedent for plugins with non-trivial local state is the subdirectory pattern: hindsight uses `$HERMES_HOME/hindsight/config.json` and writes through `get_hermes_home() / "hindsight"` (`/Volumes/LEXAR/Claude/hermes-agent/plugins/memory/hindsight/__init__.py:305,612-615`). The flat-file pattern (honcho) only fits plugins with single-file local state; LCM has four classes of state and needs a directory.

Naming the directory after the package (`lossless-hermes`) keeps install-name, entry-point name, and data-dir name in lockstep — operators get one mental model, not three. This is a deliberate departure from the `lossless/` shorthand in `dependencies.md:164-170`; we treat that document as needing a follow-up edit to align.

OpenClaw migration becomes a literal file-tree copy with no renames, because the inner structure (`lcm.db`, `credentials/voyage-api-key`, large-file blobs) matches OpenClaw's layout. This is the dominant migration constraint — existing OpenClaw users are the primary v0.1 audience.

`get_hermes_home()` (`hermes_constants.py:14-106`) is the canonical resolution path and is profile-aware: `$HERMES_HOME/lossless-hermes/` automatically scopes to the active Hermes profile, giving us per-profile LCM state for free.

## Consequences

- **Directory creation is the plugin's responsibility.** `LCMEngine.__init__` (or `on_session_start`) must `Path(get_hermes_home() / "lossless-hermes").mkdir(parents=True, exist_ok=True)` and create the four subdirectories before any I/O.
- **Migration tooling required.** Ship a `hermes lcm migrate-from-openclaw` CLI subcommand (via `register_cli_command`) that performs the file-tree copy from `~/.openclaw/` to `$HERMES_HOME/lossless-hermes/`. Idempotent; refuses to overwrite an existing `lcm.db`.
- **Credentials path is canonical.** The Voyage-key file source resolves to `$HERMES_HOME/lossless-hermes/credentials/voyage-api-key` (NOT `lossless/credentials/voyage-api-key` as `dependencies.md:170` says). `dependencies.md` needs a follow-up edit; tracked in Open questions.
- **Backups rotate locally.** The `backups/` subdir holds rotating snapshots; the rotation policy (count, age) is configurable per `plugins.entries.lossless-hermes.backup.*` in `config.yaml`.
- **Large-file spill is conversation-scoped.** Splitting per `<conv_id>` keeps cleanup tractable — purging a conversation purges one directory.
- **Precluded:** No LCM files outside `$HERMES_HOME/lossless-hermes/`. We do NOT write to `$HERMES_HOME/logs/lcm/` or `$HERMES_HOME/cache/lcm/`. Centralisation is the value prop.
- **Invariant:** every plugin-internal path is derived from `get_hermes_home() / "lossless-hermes"`. No hardcoded `~/.hermes/...` strings anywhere in the codebase — they break Hermes profile mode.

## Open questions / 5% uncertainty

1. **`dependencies.md:164-170` discrepancy.** It documents `$HERMES_HOME/lossless/credentials/voyage-api-key` but this ADR mandates `$HERMES_HOME/lossless-hermes/credentials/voyage-api-key`. The ADR wins (it's the load-bearing decision); `dependencies.md` needs a follow-up edit to align. Tracked as a TODO before Phase 2 closes.
2. **Profile-mode + system Hermes daemon.** When Hermes runs under systemd with a profile, `HERMES_HOME` must be propagated explicitly (`hermes_constants.py:55-59`). If a sysadmin forgets, LCM falls back to `~/.hermes/lossless-hermes/` of the daemon user, which may not be the user's actual profile. Mitigation: document this in CONTRIBUTING and include a `hermes lcm doctor paths` subcommand that prints the resolved data dir.
3. **Symlink to OpenClaw for dual-running.** Some operators may want to keep both OpenClaw and lossless-hermes alive temporarily (verify outputs match). A symlink `$HERMES_HOME/lossless-hermes/lcm.db -> ~/.openclaw/lcm.db` is appealing but fragile — both processes writing to the same DB will race. Document: copy, don't symlink. If dual-run is needed, use the migration tool and accept divergence after the cut-over.
4. **Disk-quota collision with Hermes's `state.db`.** If a host runs out of disk while LCM is writing a backup, Hermes's own writes may fail. Mitigation: backup rotation has a configurable max-size; we expose `backup.max_total_mb` and refuse to write a new backup if the budget is exceeded.
