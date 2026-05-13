---
name: Port issue
about: Top-level `ContextAssembler.assemble(...)` orchestrating steps 4–7
title: '[epic-03] assembler: orchestrate full assemble() pipeline'
labels: 'port'
---

## Source (TypeScript)
- File: `src/assembler.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 1102–1332 (`assemble` body)
- Function(s)/class(es): `ContextAssembler.assemble`, plus the inline post-processing (normalization 1250–1274, clean-empty-assistants 1283–1293, pre-sanitize hashing 1295–1300, sanitize call 1301, return at 1320–1332).

Engine-side seam:
- File: `src/engine.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 6648–6832 (`engine.assemble`, with `safeFallback`, prefix-stability snapshot, deferred-debt consumption — see ADR-010 for what changes on the Python side).

## Target (Python)
- `src/lossless_hermes/assembler.py` — `ContextAssembler.assemble(inp: AssembleInput) -> AssembleResult` (top-level orchestration).
- `src/lossless_hermes/engine/assemble.py` — `_AssembleMixin._assemble(session_id, messages, token_budget, prompt=None)` (engine-side wrapper that handles safe-fallback + prefix-stability snapshot + deferred-debt consumption hook).
- Estimated LOC: ~250 (assembler orchestration) + ~150 (engine-side wrapper) = 400.

## Background

`assemble` is the integration point that calls every other piece this epic builds:

1. **Read context items** — `summary_store.get_context_items(conversation_id)`.
2. **Resolve** — call `_resolve_items` (#03-04).
3. **Compute fresh-tail boundary** — call `_resolve_fresh_tail_ordinal` (#03-05).
4. **Compute orphan-stripping ordinal** — defaults to `fresh_tail_ordinal`; callers override with stable boundary from `LCMEngine._stable_orphan_stripping_ordinals_by_conversation`.
5. **Index all tool-result ordinals** (1143–1155).
6. **Split** evictable vs fresh_tail at the boundary.
7. **(#628 stub-tier — NO-OP in v0.1.0 per ADR-030)** if `stub_large_tool_payloads` flag is True, log a warning and skip. Real implementation lands in v0.2.0.
8. **Budget walk** — call `_budget_walk` (#03-06).
9. **Append fresh tail** — `selected = evictable_kept + fresh_tail`.
10. **Build overflow diagnostics** (1236, helpers `topContributors`, `buildRefDuplicateClusters`, `buildMessageContentDuplicateClusters` at 877–954).
11. **Filter non-fresh assistant tool calls** — call `_filter_non_fresh_assistant_tool_calls` (#03-07).
12. **Normalize assistant content** (1250–1274) — string-content → `[{type:"text", text}]`; filter blank text blocks.
13. **Clean empty assistant turns** (1283–1293) — drop blank-only or thinking-only assistants (`is_thinking_only_content`, line 97).
14. **Pre-sanitize hashing** (1295–1300) — SHA-256 of evictable, fresh_tail, combined cleaned. For debug.
15. **Sanitize tool-use ↔ tool-result pairing** — call `sanitize_tool_use_result_pairing` (#03-07).
16. **Return** `AssembleResult(messages, estimated_tokens, stats, debug)` with `estimated_tokens = evictable_tokens + tail_tokens`.

## Engine-side wrapper (`_AssembleMixin._assemble`)

The engine-level wrapper handles policy that lives above the assembler. From `docs/porting-guides/engine.md` §"assemble(params)":

1. Ignored-session / no-conversation → return original messages as `safe_fallback()` (strip assistant prefill tails).
2. Consume any deferred debt that's safe to drain — `_maybe_consume_deferred_compaction_debt_for_assemble` (per ADR-010; the call itself is Epic 04 territory but the seam is here).
3. Resolve cache-aware state, pick `stable_orphan_stripping_ordinal` (hot-cache uses last known, cold clears it).
4. Load context items. If only raw messages and they trail live → fall back to live.
5. Delegate to `assembler.assemble(...)`.
6. Sanity checks: empty result → fallback; no user turn in result → fallback (prevent prefill errors).
7. Update `self._previous_assembled_messages_by_conversation` snapshot for next-turn prefix-stability diagnostics.
8. Return `{messages, estimated_tokens}`.

### State touched on the shell class

```python
self._previous_assembled_messages_by_conversation: dict[int, AssemblePrefixSnapshot] = {}
self._stable_orphan_stripping_ordinals_by_conversation: dict[int, int] = {}
```

Both declared on `LCMEngine.__init__` per Epic 02.

## `safe_fallback`

Strips assistant prefill tails from the live `messages` list (the last assistant message must not be a partial-prefill, which Hermes-side bookkeeping can leave behind). Read the TS source in `engine.ts:safeFallback` (search the file for the name; not in the line ranges quoted above). Mechanical port — small.

## `AssembleInput` and `AssembleResult` (final shape)

```python
@dataclass(slots=True)
class AssembleInput:
    conversation_id: int
    token_budget: int
    fresh_tail_count: int = 8
    fresh_tail_max_tokens: int | None = None
    prompt: str | None = None
    prompt_aware_eviction: bool = True
    orphan_stripping_ordinal: int | None = None
    # Deferred per ADR-030; accepted for forward-compat but treated as no-op + warning.
    stub_large_tool_payloads: bool = False

@dataclass(slots=True)
class AssembleResult:
    messages: list[dict]
    estimated_tokens: int
    stats: dict      # {raw_message_count, summary_count, total_context_items}
    debug: dict | None = None  # ~16 fields incl. overflow_diagnostics (see assembler-compaction.md)
```

## Dependencies
- Depends on: #03-04, #03-05, #03-06, #03-07 (all assembler pieces).
- Blocks: #03-09 (always-on substitution wires `_AssembleMixin._assemble` into the substitution path).

## Acceptance criteria

### `ContextAssembler.assemble`

- [ ] Steps 1–16 above execute in order with no missing seams.
- [ ] Empty `context_items` → returns `AssembleResult(messages=[], estimated_tokens=0, stats={...})` (caller's safe_fallback handles this).
- [ ] `stub_large_tool_payloads=True` logs a `warning("stub-tier deferred to v0.2.0 per ADR-030")` and runs the rest of the pipeline normally with no stub substitution.
- [ ] `estimated_tokens` correctly equals `evictable_kept_tokens + fresh_tail_tokens`.
- [ ] `debug` field populated when caller requests it (gated behind a flag, since the SHA-256 hashes are non-trivial).
- [ ] Output messages list passes a sanity check: no `tool_result` without preceding matching `tool_use`; no empty/thinking-only assistant turns.

### `_AssembleMixin._assemble` (engine-side)

- [ ] Ignored-session bypass: returns `safe_fallback(messages)` unchanged.
- [ ] Conversation not found: returns `safe_fallback(messages)` unchanged.
- [ ] Empty assembler result triggers fallback.
- [ ] Assembler result with no user turn triggers fallback (per `engine.ts` invariant — prevents Anthropic prefill errors).
- [ ] `_previous_assembled_messages_by_conversation[conv_id]` snapshot updated on every successful assemble.
- [ ] Stable orphan-stripping ordinal: when hot-cache (cache state == HIT), reads from `_stable_orphan_stripping_ordinals_by_conversation`; when cold, clears the entry and uses `fresh_tail_ordinal` directly. Cache-state read is a stub for Epic 04 (placeholder returns `COLD` by default).
- [ ] Per-session lock acquired before any DB read.
- [ ] Function signatures match `docs/porting-guides/engine.md` §"assemble(params)".

### Tests

- [ ] All TS unit tests covering `assemble` end-to-end (the bulk of `test/engine.test.ts` `// ── assemble` cluster) ported to `tests/test_engine_assemble.py`.
- [ ] All TS unit tests covering `ContextAssembler.assemble` (`test/assembler*.test.ts` end-to-end) ported to `tests/test_assembler.py`.
- [ ] An end-to-end fixture: seed DB with a known context-items DAG, call `engine._assemble(...)`, assert message list + token count + debug-hashes are byte-identical to the TS reference output for the same DAG.
- [ ] `pytest tests/test_assembler.py tests/test_engine_assemble.py` passes locally + on GitHub CI.
- [ ] No new mypy errors.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests

- Empty conversation → safe_fallback returns input messages unchanged.
- 50-message conversation, budget large enough for full-fit → mode is `full-fit`, all items kept.
- 500-message conversation, tight budget, no prompt → mode is `chronological`, only newest fit.
- 500-message conversation with a strong prompt → mode is `prompt-aware`, items containing prompt terms preferred.
- Tool-use chain across multiple turns: fresh tail keeps the latest tool_result; older orphans stripped.
- Prefix-stability test: two consecutive `_assemble` calls with identical inputs produce byte-identical first-N messages.
- Stale `_previous_assembled_messages_by_conversation` snapshot does not leak across `on_session_reset`.
- `stub_large_tool_payloads=True` warning logged, no exception, no stub substitution applied.

## Estimated effort
**12 hours**. The orchestration is mechanical given the sub-pieces (#03-04 through #03-07), but the engine-side wrapper (`_AssembleMixin._assemble`) has cross-cutting concerns (cache-state, prefix-stability snapshot, deferred-debt seam) that require careful read of `engine.ts:6648–6832`.

## Confidence
**85%**. Risk concentrates on the engine-side wrapper's interactions with Epic 04 (compaction telemetry, deferred-debt) — stub those out cleanly so this issue ships on its own. The assembler orchestration itself is straightforward given the sub-issues.
