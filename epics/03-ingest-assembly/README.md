# Epic 03 — Ingest + Assembly

**Status: closed** — all 10 issues merged (PRs #35, #39, #41–#42, #44, #46, #48, #50–#52, #56); v0.1.0 release gate.

End-to-end per-turn ingest from `post_llm_call` hook through the DB, plus the full context-assembly pipeline that produces a prompt-budget-fit message list every turn (post-v0.2.0 substitution path per ADR-030).

## Goal

Make LCM's two load-bearing per-turn operations work on Hermes:

1. **Ingest** — every new message that lands in `conversation_history` is persisted to `messages` / `message_parts` / `context_items` exactly once. Diff-based; no upstream Hermes patches required (per ADR-009).
2. **Assembly** — every turn, BEFORE the LLM call, LCM rebuilds the message list from the DAG (`context_items` + summaries) under a token budget. Three selection modes (full-fit / prompt-aware BM25-lite / chronological), orphan-tool-call stripping, tool_use ↔ tool_result repair (per ADR-010).

The recall-policy text (`LOSSLESS_RECALL_POLICY_PROMPT`) is injected into the current turn's user message via the same `pre_llm_call` hook that handles assembly side effects (per ADR-014).

## Deliverables

- `src/lossless_hermes/estimate_tokens.py` (~80 LOC, code-point-aware token estimator per ADR-021).
- `src/lossless_hermes/engine/ingest.py` (`_IngestMixin`) — per-turn diff-on-each-turn ingest + `handle_tool_call` belt-and-suspenders.
- `src/lossless_hermes/assembler.py` (~1,200 LOC) — full assembler **without** #628 stub-tier (deferred to v0.2.0 per ADR-030).
- `src/lossless_hermes/transcript_repair.py` — `sanitize_tool_use_result_pairing` (final repair pass; prerequisite for `assemble()` return).
- `src/lossless_hermes/engine/assemble.py` (`_AssembleMixin`) — top-level orchestration with `safe_fallback`, prefix-stability snapshotting, deferred-debt consumption seam.
- Always-on assembly mechanism — Option B (`preassemble` ABC patch) preferred path; Option A (force `compress=True` every turn) fallback gated behind `experimental.always_on_via_compress` config flag (per ADR-010).
- `pre_llm_call` hook that returns the (reworded-for-user-voice) recall-policy prompt (per ADR-014).

## Dependencies

- **Epic 01 (Storage)** — `messages`, `message_parts`, `context_items`, `summaries` schema + `ConversationStore` / `SummaryStore` must be in place before any ingest write or assembler read can run.
- **Epic 02 (Engine skeleton)** — `LCMEngine` shell class with `__init__` state (`_session_locks`, `_last_seen_message_idx`, `_previous_assembled_messages_by_conversation`, `_stable_orphan_stripping_ordinals_by_conversation`), mixin composition (ADR-027), and `hermes_bridge.py` (ADR-024).

## Blocks

- **Epic 04 (Compaction)** — leaf/condensed compaction relies on `assembler.resolve_items()` to read the DAG and on `estimate_tokens` for budget math. Compaction telemetry also reads ingest's `last_seen_message_idx`.

## Critical path: YES

This epic delivers the per-turn substitution that defines LCM. Without it, the port is a compaction-only system equivalent to Hermes's existing compressor. Epic 04 and Epic 05 cannot integrate-test without ingest+assembly.

## Estimated total effort

**3 weeks (~80–100 hours)** across 10 issues. Approximate distribution:

- Token estimator + tests: ~6 h
- Ingest (diff hook + handle_tool_call fallback): ~12 h
- Assembler (resolve_items, fresh-tail, budget-walk, orphan stripping, sanitize, orchestration): ~50 h
- Always-on substitution hook (preassemble path + force-compress fallback): ~12 h
- Recall-policy injection: ~6 h
- Buffer for integration tests + cross-issue glue: ~10 h

## Confidence: 85%

The TS algorithm is fully documented (`docs/porting-guides/assembler-compaction.md`) and the ingest mechanism is settled (ADR-009 at 85%). The remaining uncertainty lives in ADR-010 — always-on assembly is gated on the upstream Hermes `preassemble` ABC patch. v0.1.0 can ship with the experimental `should_compress=True` fallback documented, but production-grade always-on assembly requires the upstream merge. If the patch is rejected upstream, this epic still delivers — but the substitution path runs in experimental mode and v1.0 slips.

## Issues

| # | Title | Hours | Confidence |
|---|---|---:|---:|
| 03-01 | Port `estimate-tokens.ts` per ADR-021 | 6 | 95% |
| 03-02 | `post_llm_call` ingest diff-on-turn hook | 8 | 85% |
| 03-03 | `handle_tool_call` ingest fallback | 4 | 80% |
| 03-04 | Port `assembler.resolveItems()` | 10 | 90% |
| 03-05 | Port `resolveFreshTailOrdinal()` | 5 | 95% |
| 03-06 | Port the three budget-walk selection modes | 12 | 90% |
| 03-07 | Port `filterNonFreshAssistantToolCalls` + `sanitizeToolUseResultPairing` | 10 | 85% |
| 03-08 | Top-level `ContextAssembler.assemble()` orchestration | 12 | 85% |
| 03-09 | Wire always-on substitution per ADR-010 | 12 | 60% |
| 03-10 | `pre_llm_call` recall-policy injection per ADR-014 | 6 | 90% |

## Source pin

All line numbers in issues reference **`lossless-claw` `pr-613` HEAD (`1f07fbd`)** unless otherwise noted. **PR #628 stub-tier is explicitly deferred to v0.2.0** (per ADR-030) — `apply_stub_substitution` and its sidecar fields are NOT in this epic's scope. The assembler signature accepts `stub_large_tool_payloads: bool = False` for forward-compat but the v0.1.0 implementation MUST treat it as a no-op (defensive: log a warning if `True` is passed).
