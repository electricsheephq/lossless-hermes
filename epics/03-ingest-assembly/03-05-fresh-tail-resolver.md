---
name: Port issue
about: Port `resolveFreshTailOrdinal()` — fresh-tail boundary calculation
title: '[epic-03] assembler: port resolveFreshTailOrdinal boundary calculation'
labels: 'port'
---

## Source (TypeScript)
- File: `src/assembler.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 983–1032 (function body) + 1132 (call site inside `assemble`)
- Function(s)/class(es): `resolveFreshTailOrdinal`

## Target (Python)
- File: `src/lossless_hermes/assembler.py`
- Estimated LOC: ~70

## Background

`resolve_fresh_tail_ordinal(resolved, fresh_tail_count, fresh_tail_max_tokens) -> int` computes the boundary ordinal that separates "always-preserved newest messages" from "evictable older messages." The fresh tail is the floor: budget-walk operates only on items with `ordinal < fresh_tail_ordinal`.

## Algorithm (from `docs/porting-guides/assembler-compaction.md` §"Step-by-step" step 3)

Walks raw-message items from newest to oldest, protecting up to `fresh_tail_count` items. Stops early if `fresh_tail_max_tokens` would be exceeded **but always preserves at least the newest message** (the user's current turn must never be evicted).

Pseudocode:

```python
def resolve_fresh_tail_ordinal(
    resolved: list[ResolvedItem],
    fresh_tail_count: int,
    fresh_tail_max_tokens: int | None,
) -> int:
    """Maps to assembler.ts:983–1032.

    Returns the smallest ordinal `o` such that all items with `ordinal >= o`
    are preserved as the fresh tail. Walks newest → oldest.
    """
    # Index raw-message items in reverse (newest first).
    message_items_desc = [item for item in reversed(resolved) if item.is_message]
    if not message_items_desc:
        # No raw messages → fresh tail is empty; ordinal points past the end.
        return len(resolved)

    preserved_count = 0
    preserved_tokens = 0
    boundary_ordinal = message_items_desc[0].ordinal  # newest is always kept

    for item in message_items_desc:
        if preserved_count >= fresh_tail_count:
            break
        next_tokens = preserved_tokens + item.tokens
        # Always keep the newest, even if it alone exceeds the cap.
        if (
            fresh_tail_max_tokens is not None
            and preserved_count > 0  # i.e. we already kept at least one
            and next_tokens > fresh_tail_max_tokens
        ):
            break
        preserved_count += 1
        preserved_tokens = next_tokens
        boundary_ordinal = item.ordinal

    return boundary_ordinal
```

## Invariants from the TS source

- **Newest is always kept**, even if it alone exceeds `fresh_tail_max_tokens`. The user's current turn cannot be evicted.
- **Default `fresh_tail_count = 8`** (per `assembler.ts:128–177` AssembleContextInput defaults). LCM tests verify this default.
- **`fresh_tail_max_tokens` is optional** — when `None`, only `fresh_tail_count` gates.
- **Only raw-message items count**. Summaries between raw messages are skipped during the walk but DO end up included if their ordinal is `>= boundary_ordinal` (because the splitter in `assemble` uses `>= boundary` not "is_message and >= boundary").
- **Boundary is the ordinal of the OLDEST kept item**, not "first ordinal past the tail." The splitter at line 1156 uses `>= fresh_tail_ordinal` as the fresh-tail predicate.

## Edge cases to test

- Empty `resolved` list — return `len(resolved)` (0).
- All-summary items (no raw messages) — return `len(resolved)` (effectively empty fresh tail; everything is evictable).
- Single message — return its ordinal.
- `fresh_tail_count = 0` — return sentinel (`EMPTY_FRESH_TAIL_ORDINAL`); disables the fresh tail entirely. Matches TS `assembler.ts:988-990` which returns `Infinity` when `freshTailCount <= 0` (the 5 TS integration tests at `lcm-integration.test.ts:1510/1567/1628/1688/1761` configure `freshTailCount: 0` to disable the mechanism). PR #45 reviewer-confirmed.
- `fresh_tail_max_tokens` smaller than the newest message — still keep newest.
- `fresh_tail_count` larger than the message count — keep everything.
- Mixed messages + summaries — summaries between kept messages get included.

## Dependencies
- Depends on: #03-04 (`ResolvedItem` shape with `is_message`, `tokens`, `ordinal`).
- Blocks: #03-06 (budget walk uses the boundary), #03-07 (orphan stripping uses the boundary), #03-08 (orchestration).

## Acceptance criteria

- [ ] `resolve_fresh_tail_ordinal` is a `@staticmethod` on `ContextAssembler` (or module-level — pick one and stay consistent).
- [ ] Default `fresh_tail_count = 8` applies when caller passes no value (or matches the value in `AssembleInput` dataclass).
- [ ] Newest message is always kept, even if alone over the token cap.
- [ ] `fresh_tail_count = 0` returns sentinel (TS canonical — disables fresh tail; see Edge cases above).
- [ ] Empty `resolved` returns `len(resolved)` (= 0); a test asserts no division-by-zero or out-of-range.
- [ ] All-summary input returns `len(resolved)`.
- [ ] Summaries between kept raw messages end up inside the fresh-tail slice via the `>= boundary` splitter (verify in a #03-08 integration test, not here).
- [ ] All TS unit tests covering `resolveFreshTailOrdinal` (search `test/assembler*.test.ts` for `freshTail` / `fresh_tail`) have ported pytest equivalents under `tests/test_assembler_fresh_tail.py`.
- [ ] `pytest tests/test_assembler_fresh_tail.py` passes locally + on GitHub CI.
- [ ] No new mypy errors.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests

- All eight edge cases above as explicit fixtures.
- Fuzz: random sequences of messages with random token counts; assert returned ordinal is `<=` the ordinal of the newest message AND `>= 0`.
- Regression: fixture from a real LCM session (port from `test/assembler*.test.ts` if a fixture exists).

## Estimated effort
**5 hours**.

## Confidence
**95%**. Pure function with clear invariants. Well-tested in TS. The only residual risk is off-by-one on the boundary semantic (`>=` vs `>`) — covered by the splitter integration in #03-08.
