---
name: Port issue
about: Port `assembler.resolveItems()` — hydrate ResolvedItem from context_items JOIN messages/summaries
title: '[epic-03] assembler: port resolveItems hydration'
labels: 'port'
---

## Source (TypeScript)
- File: `src/assembler.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 1374–1466 (`resolveMessageItem` 1374–1448, `resolveSummaryItem` 1450–1466). Plus the helpers it depends on: `parseJson` 188–197, `getOriginalRole`/`getPartMetadata` 199–239, `tryRestoreOpenAIReasoning` 265–278, `toolCallBlockFromPart` 281–328, `toolResultBlockFromPart` 331–390, `toRuntimeRole` 392–418, `blockFromPart` 421–535, `contentFromParts` 538–565, `pickToolCallId` / `pickToolName` / `pickToolIsError` 568–640, `formatSummaryContent` 814–852.
- Function(s)/class(es): `resolveItems`, `resolveMessageItem`, `resolveSummaryItem`, plus the block-reconstruction helpers.

## Target (Python)
- File: `src/lossless_hermes/assembler.py`
- Estimated LOC: ~500 (most of this issue's surface; block-reconstruction is half of the assembler module)

## Background

`resolveItems(ctx_items) -> list[ResolvedItem]` is the first step of `assemble`. It hydrates the ordered `ContextItemRecord[]` from `summary_store.get_context_items(conversation_id)` into a list of `ResolvedItem`s carrying:

- `ordinal` — position in the DAG.
- `message: dict` — runtime-shaped message (role + content array).
- `tokens: int` — `estimate_tokens(content)` on the serialized form (per ADR-021).
- `is_message: bool` — flag for budget walk + orphan stripping.
- `text: str` — plain-text rendering used for BM25 scoring + duplicate-cluster diagnostics.
- DB metadata: `message_id`, `seq`, `source_role` (for messages); `summary` record (for summaries).

This is the lowest-level reading layer of the assembler. Everything downstream (budget walk, orphan stripping, sanitize) consumes `ResolvedItem`s.

## Key invariants from the TS source

From `docs/porting-guides/assembler-compaction.md` §"Step-by-step":

- **Tool-result without `toolCallId` is degraded to assistant** (line 1399). Anthropic-compatible APIs reject `tool_result` blocks missing the call id. Preserves text instead of dropping it.
- **Summary items are formatted as an XML wrapper** (`formatSummaryContent`, 814–852): `<summary id="..." kind="..." depth="..." ...><content>...</content></summary>` plus `<parents>` for `kind: "condensed"`.
- **Provider-specific keying** for tool blocks (`toolCallBlockFromPart`): Anthropic uses `input`; OpenAI uses `arguments`. The block reconstruction must pick the right key based on `metadata.rawType`.
- **OpenAI reasoning restoration** (`tryRestoreOpenAIReasoning`, 265–278): reverse OpenClaw's normalization back to `{type: "reasoning", id: "rs_..."}` for OpenAI Responses API.
- **`message_parts.metadata`** is JSON-encoded. Tolerant parse: failures return `undefined` (Python: `None`) instead of throwing.

## #628 stub-tier fields — NOT in v0.1.0 per ADR-030

The `ResolvedItem` dataclass MUST include the stub-tier fields as optional for forward-compat with v0.2.0, but `resolve_message_item` MUST NOT populate them. The fields:

```python
@dataclass(slots=True)
class ResolvedItem:
    ordinal: int
    message: dict
    tokens: int
    is_message: bool
    text: str
    message_id: int | None = None
    seq: int | None = None
    source_role: str | None = None
    summary: SummaryRecord | None = None
    # v0.2.0 (#628 stub-tier; deferred per ADR-030) — fields exist for forward-compat
    # but are NOT populated by resolve_message_item in v0.1.0.
    file_id: str | None = None
    file_byte_size: int | None = None
    stub_tool_name: str | None = None
    stub_tool_call_id: str | None = None
    file_summary: str | None = None
```

Add a comment at the top of `resolve_message_item` noting v0.1.0 leaves the stub-tier fields `None`.

## Block-reconstruction surface

The biggest sub-issue. `blockFromPart` (421–535) is a large `switch` over `part.type`:

- `reasoning` → `{type: "reasoning", id, content?}` (with `tryRestoreOpenAIReasoning` if appropriate).
- `tool_use` / `toolCall` / `function_call` → reconstructed via `toolCallBlockFromPart`.
- `tool_result` / `function_call_output` → reconstructed via `toolResultBlockFromPart`.
- `text` → `{type: "text", text}`.
- Anything else → metadata-raw fallback (preserves provider-specific shapes).

`contentFromParts` (538–565) then assembles the blocks into a content array. Special case: a single text-block user message collapses to a plain string (OpenAI Chat shape compatibility).

## Dependencies
- Depends on: #03-01 (token estimator), Epic 01 (storage — `SummaryStore.get_context_items`, `ConversationStore.get_messages_by_ids`, `SummaryStore.get_summary_by_id`).
- Blocks: #03-05, #03-06, #03-07, #03-08 (all downstream assembler issues consume `ResolvedItem`).

## Acceptance criteria

- [ ] `resolve_items(ctx_items)` returns `list[ResolvedItem]` in `ordinal` order with one entry per input item.
- [ ] `resolve_message_item` correctly reconstructs:
  - User messages (plain text + multi-block).
  - Assistant messages (text-only, tool-use, mixed).
  - Tool-result messages with `tool_call_id`.
  - Tool-result messages WITHOUT `tool_call_id` — degraded to `role: "assistant"` per line 1399 invariant.
- [ ] `resolve_summary_item` wraps summary content in `<summary id="..." kind="..." depth="..."><content>...</content></summary>` (plus `<parents>` for condensed).
- [ ] `tokens` field is computed via `estimate_tokens` from #03-01 on the serialized content.
- [ ] `text` field is the plain-text rendering used by BM25 + dedup-clusters — strips reasoning/thinking blocks (`extractMeaningfulMessageText` analog).
- [ ] Provider-specific keying (Anthropic `input` vs OpenAI `arguments`) preserved.
- [ ] OpenAI reasoning blocks survive round-trip (in via `tryRestoreOpenAIReasoning`, out via `blockFromPart`).
- [ ] **Stub-tier fields exist on `ResolvedItem` but are NEVER set** in v0.1.0 — assert via test fixture.
- [ ] `parse_json` (Python equivalent of `parseJson`) handles `None`, empty string, malformed JSON gracefully.
- [ ] All TS unit tests in `test/assembler-blocks.test.ts` have ported pytest equivalents under `tests/test_assembler_blocks.py`.
- [ ] `pytest tests/test_assembler_blocks.py` passes locally + on GitHub CI.
- [ ] No new mypy errors.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests

Port `test/assembler-blocks.test.ts` verbatim. Add fixtures for:

- Multi-block tool_result (must stay array shape — critical for Anthropic invariant flagged in `docs/porting-guides/assembler-compaction.md` §"Critical invariants").
- Tool-result without `tool_call_id` — assert role degrades to `"assistant"`.
- Summary item with `kind: "condensed"` + parents — assert `<parents>` block appears in XML.
- Provider keying: Anthropic part with `input` field, OpenAI part with `arguments` field — both round-trip correctly.
- Empty content message — skipped or rendered with neutral text per TS behavior.
- `metadata` JSON missing or malformed — falls back without raising.

## Estimated effort
**10 hours**. Block reconstruction is mechanical but broad — many small per-type cases. Plan to port `blockFromPart` last and use the existing tests as the spec.

## Confidence
**90%**. The TS source is well-tested via `assembler-blocks.test.ts`; the port is large but each block-type branch is independent. Residual risk: provider-keying edge cases for hybrid tool blocks (e.g., a single message that contains both Anthropic and OpenAI-shaped parts). Mitigated by the test fixtures.
