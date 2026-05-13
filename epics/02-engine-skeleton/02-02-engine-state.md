---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] engine: initialize all LcmContextEngine state fields'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/engine.ts`
- Lines: ~1734–1900 (field declarations on the `LcmContextEngine` class)
- Function(s)/class(es): every `private <field>` declaration plus constructor assignments

## Target (Python)
- File: `src/lossless_hermes/engine/__init__.py` (specifically `_init_state_fields()` method)
- Estimated LOC: ~80 (just the field initialization block)

## Summary

Port every state field from `LcmContextEngine` per the table in `docs/porting-guides/engine.md` lines 23–49. Drop the JSONL-specific fields per the same guide (line 51): `lastFullReadFileState`, `recentBootstrapImportsByConversation`, `oversizedAutoRotateCheckpointByQueueKey`, `afterTurnReconcileFullReadStates`, `largeFileTextSummarizer`.

This issue is purely declarative — initializes every dict/set/map the engine carries, with correct types and empty defaults. Functional behavior comes from other issues.

## State fields to declare (per engine.md table)

| Python field | Type | Purpose |
|---|---|---|
| `self.info` | `ContextEngineInfo` | Identity + `owns_compaction` flag (true unless migration failed) |
| `self.migrated` | `bool` | Set at construction once `run_lcm_migrations` succeeds |
| `self.fts5_available` | `bool` | Probed at construction (Epic 01 dependency) |
| `self.ignore_session_patterns` | `list[re.Pattern]` | Compiled from `config.ignore_session_patterns` |
| `self.stateless_session_patterns` | `list[re.Pattern]` | Compiled from `config.stateless_session_patterns` |
| `self._session_locks` | `defaultdict[str, asyncio.Lock]` | Per-session mutex (see issue 02-08) |
| `self._previous_assembled_messages_by_conversation` | `dict[int, AssemblePrefixSnapshot]` | Last assembled message list per conversation (prefix-stability diagnostics) |
| `self._stable_orphan_stripping_ordinals_by_conversation` | `dict[int, int]` | Stable boundary for orphan-tool-result stripping |
| `self._circuit_breaker_states` | `dict[str, CircuitBreakerState]` | Auth-failure breakers per session/provider scope (issue 02-09) |
| `self._cache_context_unknown_logged` | `set[int]` | Per-process dedupe for cache-context-unknown info log |
| `self._last_seen_message_idx` | `dict[str, int]` | Keyed by `session_id`. Tracks Hermes `conversation_history` length for diff-on-each-turn ingest (ADR-009). Replaces TS bootstrap fast-path state. |

## Dropped fields (JSONL-specific — per engine.md line 51)

- `lastFullReadFileState` — JSONL session-file checkpoint. Hermes has no JSONL.
- `recentBootstrapImportsByConversation` — bootstrap diagnostics. Drops with ADR-011.
- `oversizedAutoRotateCheckpointByQueueKey` — auto-rotate guard. Drops.
- `afterTurnReconcileFullReadStates` — transcript reconcile. Drops.
- `largeFileTextSummarizer` (and the resolved flag) — lazy model summarizer. Replaced by constructor-injected summarizer (issue 02-01).

## Implementation outline

```python
# In LCMEngine.__init__ (issue 02-01 sets up the method skeleton):

def _init_state_fields(self) -> None:
    """Initialize all in-memory state per engine.md table."""
    import re
    from collections import defaultdict
    import asyncio

    # Identity
    self.info = ContextEngineInfo(
        name="lcm",
        version="0.1.0",
        owns_compaction=self.migrated,  # degrades to no-op if migration failed
    )

    # Feature detection
    self.fts5_available = self.db.fts5_available  # Epic 01 store exposes this

    # Compiled patterns
    self.ignore_session_patterns = [
        re.compile(p) for p in self.config.ignore_session_patterns
    ]
    self.stateless_session_patterns = [
        re.compile(p) for p in self.config.stateless_session_patterns
    ]

    # Per-session sync infra (issue 02-08 fills in the lock dict semantics)
    self._session_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # Assembly state
    self._previous_assembled_messages_by_conversation: dict[int, Any] = {}
    self._stable_orphan_stripping_ordinals_by_conversation: dict[int, int] = {}

    # Circuit breaker (issue 02-09)
    self._circuit_breaker_states: dict[str, CircuitBreakerState] = {}

    # Logging dedup
    self._cache_context_unknown_logged: set[int] = set()

    # Ingest state (ADR-009: post_llm_call diff)
    self._last_seen_message_idx: dict[str, int] = {}

    # NOTE: AssemblePrefixSnapshot, ContextEngineInfo, CircuitBreakerState are
    # defined in src/lossless_hermes/types.py per ADR-024.
```

## Dependencies
- Depends on: 02-01 (the constructor that calls `_init_state_fields()`)
- Blocks: 02-03 (lifecycle), 02-05 (should_compress reads `_last_seen_message_idx`), 02-08 (session locks — initializes the dict but doesn't yet enforce the per-session semantics), 02-09 (circuit breaker — same)

## Acceptance criteria
- [ ] All 11 state fields from the engine.md table (minus the 5 dropped JSONL fields) are initialized in `_init_state_fields()`
- [ ] Type annotations match the table; mypy passes
- [ ] No JSONL-derived fields are declared
- [ ] `engine._session_locks["foo"]` returns a fresh `asyncio.Lock` (defaultdict semantics)
- [ ] `engine._circuit_breaker_states == {}` initially
- [ ] `engine._last_seen_message_idx == {}` initially
- [ ] Type stubs for `ContextEngineInfo`, `CircuitBreakerState`, `AssemblePrefixSnapshot` defined in `src/lossless_hermes/types.py`
- [ ] `pytest tests/test_engine_state.py` passes

## Tests
- `tests/test_engine_state.py::test_state_field_defaults` — instantiate; assert every field is its expected default (`{}`, `set()`, etc.)
- `tests/test_engine_state.py::test_no_jsonl_fields` — assert `hasattr(engine, "lastFullReadFileState") is False` for each of the 5 dropped fields (regression guard)
- `tests/test_engine_state.py::test_pattern_compilation` — pass `ignore_session_patterns: ["^test-.*"]` in config; assert `engine.ignore_session_patterns[0].match("test-foo")` works
- `tests/test_engine_state.py::test_session_lock_dict_creates_on_demand` — read `engine._session_locks["new_session"]`; assert it's an `asyncio.Lock`

## Estimated effort
4 hours

## Confidence
95% — pure declarative work. The only minor decision is whether `ContextEngineInfo` etc. should be Pydantic models, dataclasses, or NamedTuples. This issue chooses `dataclass` (Python-idiomatic, no Pydantic overhead for a leaf-utility type).
