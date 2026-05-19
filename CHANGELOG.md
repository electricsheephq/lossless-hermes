# Changelog

All notable changes to `lossless-hermes` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/electricsheephq/lossless-hermes/releases/tag/v0.1.0
