# Changelog

All notable changes to `lossless-hermes` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] — 2026-05-19

Patch release. Fixes two coupled defects in the diff-ingest cursor
(`_last_seen_message_idx`) reported in
[#130](https://github.com/electricsheephq/lossless-hermes/issues/130),
reimplemented from the production-tested sibling project `hermes-lcm`
(commits `79629c2`/#111, `17578a0`/#113, `4bc6b9c`/#1).

### Fixed

- **Restart no longer re-ingests an existing transcript.** The diff-ingest
  cursor `_last_seen_message_idx` is a process-local dict that is never
  persisted, and `on_session_start` had no cursor-restore logic. On a
  gateway restart the cursor reset to `0`, so the next `post_llm_call`
  re-diffed the entire replayed `conversation_history` from the start.
  Because `messages` has no `UNIQUE` constraint on `identity_hash` (only
  `UNIQUE(conversation_id, seq)`) and the ingest path assigns a fresh
  `seq = get_max_seq() + 1` per row with no dedup lookup, every restart
  would silently re-ingest the whole transcript with new `seq`s. Before
  v0.1.2 this symptom was *masked*: a separate durability bug rolled back
  every session's ingest on connection close, so a restart found an empty
  store and re-ingestion could not manifest (the restart instead caused
  total ingest loss). With v0.1.2's durability fix now in place the store
  survives a restart, so the restart re-ingestion path is fully live and
  this fix is load-bearing. `_IngestMixin._reconcile_ingest_cursor` now
  reconciles the cursor against the durable `messages` store the first time
  a process sees a session: it walks the incoming history and counts the
  longest leading run of persistable messages whose `(role, content)`
  identity matches the stored rows in `seq` order, and sets the cursor past
  that proven-replay prefix. The reconciliation is replay-evidence-gated —
  a post-restart turn that shares no prefix with the durable transcript
  leaves the cursor at `0` so its messages still ingest (no data loss).
- **Compaction no longer silently stops ingest.** The cursor is an absolute
  index into the live message list. When a compaction entry point
  substituted a hierarchical-summary list for older raw turns (a DAG
  substitution), the cursor — left at the pre-compaction length — could
  become permanently `>= len(new_list)`, so `_do_ingest_history_diff`
  early-returned on every subsequent turn and ingest stopped for the rest
  of the session. Both compaction entry points (`compress()` in
  `engine/compact.py` and `preassemble()` in `engine/assemble.py`) now call
  `_CompactMixin._reset_ingest_cursor_after_compaction`, which resets the
  cursor **only when a genuine compaction / DAG substitution actually
  occurred** — detected by a content-identity comparison of the leading
  region, not by a list-length test. A non-compaction reshape that merely
  shortens the list (notably `_safe_fallback` stripping trailing
  `assistant` messages during assembly) does *not* reset the cursor, and a
  same-length compaction substitution (N raw turns → N summary+tail
  messages) *does*. Because Hermes's `compress` / `preassemble` ABC
  signatures do not pass `session_id`, the session is resolved via
  `_infer_session_id`; when inference fails the reset is *skipped* rather
  than writing a bogus empty-string key.

### Notes

- **`identity_hash` is not `UNIQUE`.** [ADR-009](./docs/adr/009-per-message-ingest.md)
  §Consequences describes `identity_hash` as "the `UNIQUE` constraint on
  `messages.identity_hash`". It is not: both the TS source
  (`lossless-claw` at the pinned commit `1f07fbd`,
  `src/db/migration.ts:1161`) and this port (`db/migration.py:455`) define
  only a plain index `messages_conv_identity_hash_idx`. The Python port is
  a faithful copy — the schema-diff CI gate is correctly green — so this is
  an ADR overstatement, not a port-fidelity gap. No schema migration is
  added in this patch; the cursor reconciliation fix stands on its own.
- **`preassemble` is ADR-032-slated for demotion.** [ADR-032](./docs/adr/032-per-turn-assembly-not-required.md)
  supersedes ADR-010 and decides per-turn pre-assembly is not required,
  demoting `preassemble`. Its removal is separate v0.2.0 work; this patch
  only makes the compaction cursor reset correct for the `compress()` +
  `preassemble()` paths that exist in v0.1.x today.

## [0.1.2] — 2026-05-19

Patch release. Fixes a **P0 data-durability bug**: prior to this release LCM
did not durably persist any conversation data — every message ingested during
a session was silently rolled back when the session's database connection
closed. The "lossless" context engine was, in fact, losing everything on
session close, with no error.

### Fixed

- **LCM now durably persists ingested conversation data.** The single
  sanctioned connection factory, `db/connection.py`, opened its stdlib
  `sqlite3` connections without specifying `isolation_level`, leaving Python's
  default `isolation_level=""`. In that mode the first `INSERT` / `UPDATE` /
  `DELETE` silently opens an implicit *deferred* transaction that the driver
  never auto-commits. The first turn of a session runs
  `ConversationStore.create_conversation` — a bare `INSERT` with no transaction
  wrapper — which opened that implicit transaction; `conn.in_transaction` then
  stayed `True` for the rest of the session. Subsequent message persistence
  routed through `ConversationStore.with_transaction`, which saw
  `in_transaction == True`, took the `SAVEPOINT` branch, and `RELEASE
  SAVEPOINT`'d into the *uncommitted* implicit transaction — never issuing a
  `COMMIT`. Nothing on the ingest path committed, and `close_lcm_db` does not
  commit, so `conn.close()` rolled the whole session back. The same hazard
  silently discarded every single-statement write in the compaction telemetry
  and maintenance stores. The fix opens connections with
  `isolation_level=None` (autocommit / explicit-transactions mode) in both
  `open_lcm_db` and `open_db`: writes now autocommit unless inside an explicit
  `BEGIN`/`COMMIT`, so a session's data survives connection close. This
  restores the documented contract the rest of the codebase already assumed —
  the `apsw` adapter in the same file is explicitly autocommit, and
  `concurrency/worker_lock.py`, `synthesis/prompt_registry.py`,
  `doctor/cleaners.py`, and every store test fixture all state the connection
  is expected to be `isolation_level=None`. `with_transaction`'s
  `in_transaction` check now correctly distinguishes a genuinely-nested
  explicit transaction from a fresh one, and the migration ladder's
  `BEGIN EXCLUSIVE` runs from a clean autocommit state. A cross-session
  durability regression suite (ingest → close → reopen a fresh connection →
  assert the data is present), covering both the single-turn and multi-turn
  cases, is added so this class of bug — a durability property no
  write-then-read-on-the-same-connection test can detect — is caught going
  forward.

## [0.1.1] — 2026-05-19

Patch release. Fixes two production bugs surfaced by an architecture review
against the sibling project `hermes-lcm`.

### Fixed

- **Mid-session model switch no longer crashes.** Hermes-agent's
  `run_agent.py` calls `context_compressor.update_model(...)` at seven
  sites; two of them — the LM-Studio context preload and the in-place
  `/model` switch — pass an extra `api_mode=` keyword. The `ContextEngine`
  ABC's default `update_model` does not declare `api_mode`, and `LCMEngine`
  did not override the method, so every `/model` switch raised
  `TypeError: update_model() got an unexpected keyword argument 'api_mode'`.
  `LCMEngine` now overrides `update_model` to absorb `api_mode` (plus a
  `**kwargs` forward-compat sink) and delegates the `context_length` /
  `threshold_tokens` recalculation to the ABC default.
- **Recall-policy prompt no longer advertises an unregistered tool.** The
  `LOSSLESS_RECALL_POLICY_PROMPT` text — injected into the model's context
  every turn via the `pre_llm_call` hook — named `lcm_expand_query` in the
  escalation ladder, a dedicated usage block, the scope-selection rules, and
  the precision flow. Per [ADR-012](./docs/adr/012-subagent-defer.md) that
  tool is deferred to v0.2.0 and is not registered, so the model was told
  every turn to call a tool absent from its tool list. Every reference is
  rewritten to route deep recall through `lcm_describe`'s one-hop
  `expandChildren` / `expandMessages` flags (the registered path). The
  byte-verbatim tool-schema descriptions in `tools/grep.py` / `describe.py` /
  `expand.py` are intentionally left unchanged — their `lcm_expand_query`
  mentions are a deliberate, [ADR-016](./docs/adr/016-typebox-translation.md)-tested
  state and are secondary follow-up hints inside already-registered tools.

## [0.1.0] — 2026-05-19

First release. `lossless-hermes` is a feature-complete Python port of
[Lossless Claw](https://github.com/Martian-Engineering/lossless-claw) (LCM) v4.1
— pinned to upstream commit `1f07fbd` (branch `pr-613`) — running as a
[Hermes-agent](https://github.com/NousResearch/hermes-agent) plugin via the
`ContextEngine` ABC. 122 port issues across 10 epics; 109 PRs.

### Added

- **Storage** — SQLite schema + idempotent migration ladder, FTS5 + trigram
  search, `sqlite-vec` (vec0) wiring. The on-disk schema is byte-compatible
  with OpenClaw LCM, verified by a schema-diff CI gate (92/92 objects matched).
- **Engine** — `LCMEngine` implementing the Hermes `ContextEngine` ABC; the
  `/lcm` slash-command surface; per-turn ingest and always-on context assembly
  through the `pre_llm_call` / `post_llm_call` hooks.
- **Compaction** — leaf-summary and condensed-summary passes, the lossless
  conversation pyramid, anti-thrashing guard, and a synthesis circuit breaker.
- **Embeddings** — Voyage HTTP client, embedding backfill worker, hybrid
  retrieval (FTS5 ∪ vec0 with reciprocal-rank fusion + rerank-2.5), semantic
  search, and a graceful-degradation contract when `VOYAGE_API_KEY` is absent.
- **Agent tools** — 7 of LCM's 8 tools: `lcm_grep` (regex / full-text /
  verbatim / hybrid / semantic), `lcm_describe`, `lcm_get_entity`,
  `lcm_search_entities`, `lcm_expand`, `lcm_synthesize_around`, `lcm_compact`.
  Tool descriptions are byte-verbatim from the TS source, snapshot-pinned.
- **Entity + synthesis** — entity coreference pipeline, tier-aware synthesis
  dispatch, synthesis cache with leaf-change invalidation, and an audit trail.
- **Operator surface** — `/lcm` subcommands `status`, `health`, `purge`,
  `backup`, `reconcile`, `doctor` (apply + cleaners), `worker` (status + tick),
  `rotate`, `eval`, `help`; plus the `lossless-hermes import-openclaw` CLI for
  migrating an existing OpenClaw `lcm.db` without data loss.
- **Eval** — recall eval suite, LLM-as-judge ensemble harness, per-stratum
  drift detection, a secret-gated `live-eval` CI workflow, and the Voyage
  recall benchmark harness (`docs/benchmarks/voyage-recall-2026-q2.md`).
- Every scar-tissue fix from LCM's 12 audit waves is ported verbatim with
  `# LCM Wave-N` provenance comments ([ADR-029](./docs/adr/029-wave-fix-provenance.md)).

### Migration

- Existing OpenClaw LCM users: `cp ~/.openclaw/lcm.db
  "$HERMES_HOME/lossless-hermes/lcm.db" && lossless-hermes import-openclaw`.
  The migration is idempotent, refuses to overwrite without `--force`, and
  sample-validates `identity_hash` ([ADR-025](./docs/adr/025-openclaw-migration.md)).

### Deferred to v0.2.0

- `lcm_expand_query` tool and the `prepareSubagentSpawn` / `subagentEnded`
  sub-agent lifecycle ([ADR-012](./docs/adr/012-subagent-defer.md)).
- PR #628 stub-tier substitution ([ADR-030](./docs/adr/030-pr-628-stub-tier-deferred.md)).

### Known limitations

- The live +52.5pp Voyage hybrid-recall benchmark requires a provisioned
  `VOYAGE_API_KEY`; v0.1.0 ships the benchmark harness and the offline
  `fts_only` baseline, with the live confirmation as a documented operator
  step (`docs/benchmarks/voyage-recall-2026-q2.md`).
- Native Windows is out of scope; use WSL2.

[0.1.3]: https://github.com/electricsheephq/lossless-hermes/releases/tag/v0.1.3
[0.1.2]: https://github.com/electricsheephq/lossless-hermes/releases/tag/v0.1.2
[0.1.1]: https://github.com/electricsheephq/lossless-hermes/releases/tag/v0.1.1
[0.1.0]: https://github.com/electricsheephq/lossless-hermes/releases/tag/v0.1.0
