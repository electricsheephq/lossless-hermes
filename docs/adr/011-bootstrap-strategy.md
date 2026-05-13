# ADR-011: Bootstrap strategy

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 90%
**Supersedes:** —
**Superseded by:** —

## Context

OpenClaw's LCM `bootstrap(params)` method (TS `src/engine.ts:4983–5424`) imports historical JSONL transcript messages into the LCM DB on first contact with a session. It carries seven layered fast-paths (checkpoint hit, append-only, file-cache guard, cold-path, existing-conversation reconcile, session-file rollover, HEARTBEAT_OK pruning) and ~1500 LOC of JSONL-specific logic.

Hermes has **no JSONL transcript file**. Its canonical persistence is `session.db` (SQLite), which Hermes already manages internally for its own purposes. The bootstrap-on-JSONL pattern doesn't translate.

Two distinct user populations need a bootstrap story:

1. **New Hermes installs with no prior LCM state** — sessions start fresh; LCM should ingest live from `on_session_start` onward.
2. **Existing OpenClaw LCM users migrating to Hermes** — they have a populated `~/.openclaw/lcm.db` and want continuity. Spike 003 confirms `identity_hash` is byte-identical across implementations.

The constraint forcing a choice: how does LCM-on-Hermes initialize state on a new install AND provide a migration path for existing users without bloating the per-session hot path?

## Options considered

### Option A: Port the JSONL bootstrap logic verbatim

- **Description:** Translate `bootstrap()` and its 1500 LOC of fast-paths to Python, pointed at a Hermes-side transcript file.
- **Pros:** Behavioral parity with OpenClaw LCM.
- **Cons:** Hermes has no transcript file. Would require fabricating one from `session.db` queries, then re-implementing tail-scan, checkpoint reconcile, session-file rollover — all on a synthetic stream that adds no value. Significant LOC for no behavioral gain.
- **Evidence:** `docs/porting-guides/engine.md:55–80` ("DROP: every JSONL fast-path. Hermes has no JSONL session file — its persistence is session.db, which Hermes already manages.").

### Option B: Opportunistic backfill from `session.db` on `on_session_start`

- **Description:** When `on_session_start` fires, read recent rows from Hermes's `session.db` for that session_id and ingest them.
- **Pros:** Automatic.
- **Cons:** Adds latency to every session start. Couples LCM to Hermes's `session.db` schema (which is internal and may change). Most sessions are brand-new and have no history to import. Creates a cross-DB consistency hazard.
- **Evidence:** `docs/porting-guides/engine.md:78–79` mentions this as a possibility but does not recommend it. ADR-09 in engine.md (line 526) explicitly recommends against opportunistic backfill.

### Option C: Fresh-start + separate one-shot migration CLI

- **Description:** New Hermes sessions ingest live from `on_session_start` forward (no historical import). For existing OpenClaw LCM users, provide a separate `lossless-hermes import-openclaw <path>` CLI that copies `~/.openclaw/lcm.db` to `$HERMES_HOME/lossless-hermes/lcm.db` and runs idempotent migrations. Re-ingest is safe because `identity_hash` is byte-identical across implementations (spike 003).
- **Pros:** Drops ~1500 LOC of JSONL-bootstrap code. Hot path stays fast. Migration is explicit, observable, idempotent. Re-ingest cannot corrupt because of UNIQUE-on-identity_hash.
- **Cons:** Requires existing users to run one explicit command.
- **Evidence:** Spike 003 (`docs/spike-results/003-identity-hash.md`) confirms identity_hash round-trips byte-identical across TS/Go/Python implementations on all 10 test cases (line 8). Engine.md ADR-09 (`docs/porting-guides/engine.md:526`) recommends "separate CLI command rather than opportunistic on_session_start backfill. Keeps the hot path fast and makes the import an explicit operator action."

## Decision

Chosen: **Option C**

1. **New sessions.** `on_session_start` (ContextEngine ABC method, fires unconditionally on every `AIAgent.__init__` at `run_agent.py:2369`) creates the conversation row in `lcm.db` and initializes engine state. No historical import. All messages from that point ingest live via the `post_llm_call` hook (ADR-009).

2. **Existing OpenClaw LCM users.** A separate one-shot CLI:

   ```bash
   cp ~/.openclaw/lcm.db $HERMES_HOME/lossless-hermes/lcm.db
   lossless-hermes migrate
   ```

   The `lossless-hermes migrate` command runs the same idempotent migrations the engine runs on startup (`run_lcm_migrations`). Because `identity_hash` is byte-identical (spike 003), re-running ingest against the existing DB cannot produce duplicates — UNIQUE-on-identity_hash dedups any retry.

3. **No JSONL parsing at all.** `bootstrap()`'s ~1500 LOC drops from the port (engine.md "What changes" sections in `bootstrap`).

## Rationale

Hermes has no JSONL transcript file. `session.db` is canonical for Hermes's own session persistence; LCM does not need a parallel transcript stream. Engine.md (`docs/porting-guides/engine.md:55`) explicitly directs: "DROP: every JSONL fast-path (checkpoint-hit, append-only, file-cache-guard, transcript reconcile, session-file rollover). Hermes has no JSONL session file."

Spike 003 (`docs/spike-results/003-identity-hash.md:6–8`) explicitly identifies this ADR as the one it unblocks: "unblocks ADR for `lossless-hermes` re-ingest of existing OpenClaw `lcm.db` files without dedup drift." All 10 test cases (ASCII, CJK, ZWJ emoji families, embedded NUL bytes, JSON-stringified arrays, newlines/tabs, 8 KiB content, empty-string boundary) round-trip byte-identical (`docs/spike-results/003-identity-hash.md:138–149`). The Python port can read `messages.content` verbatim and pass it through `build_message_identity_hash(role, content)` — no re-derivation needed (`docs/spike-results/003-identity-hash.md:227–230`).

An explicit migration command is preferable to opportunistic on-session-start backfill because (a) it makes the import observable, (b) it doesn't slow down the hot path for the 99% of sessions that have no history to import, and (c) it surfaces failure clearly.

## Consequences

- **Drops ~1500 LOC** of TS JSONL-bootstrap code from the port. The Python class is visibly smaller.
- **`on_session_start` is simple**: create conversation row, init state, no I/O against external transcripts. Latency target: <50ms.
- **A separate CLI subcommand** must be implemented: `lossless-hermes import-openclaw [<path>]` (or `lossless-hermes migrate` if the file is already copied). This is one of the `hermes lcm-*` CLI commands documented in plugin-glue.md.
- **Re-ingest invariants** depend on identity_hash byte-identity. This is locked by the spike-003 fixture test (`docs/spike-results/003-identity-hash.md:267–289`) — any future refactor that breaks the algorithm fails loudly.
- **No transcript-GC, no auto-rotate session files** in v1 — they're JSONL-only features (engine.md drops `autoRotate*` and `rotateSessionStorage*`).
- **Legacy NULL identity_hash rows** in pre-v4 OpenClaw DBs are handled by the migration's existing rehash loop (`src/db/migration.ts:326–344` per spike 003 line 316).

## Open questions / 5% uncertainty

- **Cross-OS `~/.openclaw/lcm.db` path.** Path varies by platform (`$XDG_DATA_HOME/openclaw/` on Linux, `~/Library/Application Support/openclaw/` on macOS). The migration command should auto-detect; document the expected paths in the CLI help.
- **OpenClaw plugin-state files outside the DB** (e.g., `lcm-files/` for externalized media). Migration must copy these too. CLI should warn if `lcm-files/` is missing and large-file blob references appear in messages.
- **OpenClaw `session_key` semantics.** OpenClaw uses both `session_id` and `session_key` (the latter for cross-session identity). Hermes's `session_id` may not align cleanly. Pre-migration `/lcm reconcile-session-keys` (operator-gated) handles this manually for divergent cases.
- **Will users have multiple OpenClaw DBs?** If yes, the import command needs to accept a `--db-path` argument. Default to the conventional path; allow override.
- **HEARTBEAT_OK pruning.** OpenClaw's bootstrap calls `pruneHeartbeatOkTurns()` post-import. Hermes may or may not have heartbeat-style ack turns; engine.md (`docs/porting-guides/engine.md:567`) flags this for verification: "grep `run_agent.py` and `gateway/` for heartbeat-like patterns. If yes, port the pruner; if no, drop it entirely."
