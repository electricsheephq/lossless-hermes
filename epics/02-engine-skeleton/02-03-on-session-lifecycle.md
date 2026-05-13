---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] engine: implement on_session_start, on_session_end, on_session_reset per ADR-011'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/engine.ts`
- Lines:
  - `bootstrap(params)` — 4983–5424 (port heavily simplified per ADR-011)
  - `handleBeforeReset(params)` — 7415
  - `handleSessionEnd(params)` — 7468
- Function(s)/class(es): `bootstrap`, `handleBeforeReset`, `handleSessionEnd`, lifecycle internals

## Target (Python)
- File: `src/lossless_hermes/engine/lifecycle.py`
- Estimated LOC: ~200 (vs. ~1500 in TS — the JSONL bootstrap fast-paths all drop per ADR-011)

## Summary

Implement the three `ContextEngine` ABC lifecycle methods on `_LifecycleMixin`:

- `on_session_start(session_id, **kwargs)` — creates the `conversations` row in `lcm.db` if absent. Initializes `_last_seen_message_idx[session_id] = 0`. **No JSONL bootstrap** (ADR-011 Option C: fresh-start with separate migration CLI).
- `on_session_end(session_id, messages)` — flushes per-session state, optionally fires a final ingest snapshot (defense-in-depth for interrupted-turn coverage per ADR-009 Consequences).
- `on_session_reset()` — calls `super().on_session_reset()` (default ABC zeroes token counters), then archives the current conversation per `handleBeforeReset` semantics. Resets `_last_seen_message_idx`, clears `_stable_orphan_stripping_ordinals`.

**No `bootstrap()` body** — Hermes has no JSONL transcript file (ADR-011 Rationale). Migration from existing OpenClaw `lcm.db` is handled by a separate `lossless-hermes migrate` CLI (Epic 08).

## Method signatures

```python
# src/lossless_hermes/engine/lifecycle.py

class _LifecycleMixin:
    """Lifecycle hook implementations. Maps to engine.ts bootstrap/handleBeforeReset/handleSessionEnd
    clusters, with JSONL paths dropped per ADR-011."""

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        """Per hermes-hooks.md: fires unconditionally on every AIAgent.__init__
        (run_agent.py:2369). kwargs include hermes_home, platform, model, context_length.

        Maps to engine.ts:4983 bootstrap() — but the JSONL fast-paths all drop.
        ADR-011: Hermes has no transcript file; sessions start fresh.
        """
        if self._is_ignored_session(session_id):
            return

        # Create the conversations row if absent. No bootstrap import.
        conv_id = self.conversation_store.get_or_create_conversation(
            session_id=session_id,
            session_key=kwargs.get("session_key", session_id),
        )

        # Initialize ingest tracking (ADR-009)
        self._last_seen_message_idx[session_id] = 0

        # context_length / model — used by token estimation later
        if "model" in kwargs and "context_length" in kwargs:
            self.update_model(
                model=kwargs["model"],
                context_length=kwargs["context_length"],
            )

        logger.debug(
            "[lcm] on_session_start session_id=%s conversation_id=%s",
            session_id, conv_id,
        )

    def on_session_end(self, session_id: str, messages: list[dict]) -> None:
        """Per hermes-hooks.md: fires at REAL session boundaries (run_agent.py:5575,5600),
        NOT per-turn. Triggered by shutdown_memory_provider + commit_memory_session
        (which run on /new, /reset, and process exit).

        Maps to engine.ts:7468 handleSessionEnd.
        """
        if self._is_ignored_session(session_id):
            return

        # Defense-in-depth: catch any post-loop messages that post_llm_call missed
        # because of Ctrl-C / final_response==None (ADR-009 Consequences).
        last_idx = self._last_seen_message_idx.get(session_id, 0)
        if messages and len(messages) > last_idx:
            # Stub for Epic 03 — the actual ingest body lands there.
            # For Epic 02, log only.
            logger.debug(
                "[lcm] on_session_end has unprocessed tail: session_id=%s, last_idx=%d, len=%d",
                session_id, last_idx, len(messages),
            )

        # Clear per-session caches
        conv_id = self.conversation_store.get_conversation_id_for_session(session_id)
        if conv_id is not None:
            self._previous_assembled_messages_by_conversation.pop(conv_id, None)
            self._stable_orphan_stripping_ordinals_by_conversation.pop(conv_id, None)

        # Clear ingest tracking
        self._last_seen_message_idx.pop(session_id, None)

        # Clear per-session lock (defaultdict will recreate if needed)
        self._session_locks.pop(session_id, None)

    def on_session_reset(self) -> None:
        """Per hermes-hooks.md: fires from AIAgent.reset_session (run_agent.py:2563).
        Default ABC implementation zeroes last_*_tokens and compression_count.

        Maps to engine.ts:7415 handleBeforeReset.
        """
        # Default ABC behavior — zero token counters
        super().on_session_reset()

        # LCM-specific: archive the current conversation. The actual session_id
        # mapping comes from kwargs in the plugin-glue hook registration (which
        # passes session_id through). For the ABC method (no kwargs), we rely on
        # the most recently active session_id tracked by on_session_start.
        # If we don't have a clear "current" notion, this is a no-op and the
        # plugin-glue hook (see issue 02-07) handles the per-reset cleanup.

        # Clear per-session state for all sessions touched in this process.
        # The ABC method has no session_id; the plugin hook fires alongside
        # and handles the specific session_id case.
        logger.debug("[lcm] on_session_reset called")

    # --- helpers ---

    def _is_ignored_session(self, session_id: str) -> bool:
        """Match session_id against config.ignore_session_patterns."""
        return any(p.search(session_id) for p in self.ignore_session_patterns)
```

## Dependencies
- Depends on: 02-01 (constructor), 02-02 (state fields). Epic 01 `ConversationStore.get_or_create_conversation`.
- Blocks: Epic 03 (real ingest body fires from `on_session_end` defense-in-depth)

## Acceptance criteria
- [ ] `on_session_start("test-session-1", model="claude-sonnet-4", context_length=200000)` creates a conversations row in `lcm.db`
- [ ] Calling `on_session_start` twice with the same `session_id` is idempotent (no duplicate conversation row)
- [ ] `_last_seen_message_idx["test-session-1"] == 0` after `on_session_start`
- [ ] `on_session_end("test-session-1", messages=[{"role":"user","content":"hi"}])` clears state for that session_id; subsequent `on_session_start` re-initializes
- [ ] `on_session_reset()` calls `super().on_session_reset()` — `last_prompt_tokens` etc. are zeroed
- [ ] Session matching `ignore_session_patterns` skips conversation creation
- [ ] **NO** JSONL file is read in any of these methods (ADR-011)
- [ ] `pytest tests/test_engine_lifecycle.py` passes

## Tests
- `tests/test_engine_lifecycle.py::test_on_session_start_creates_conversation` — assert conversations row appears
- `tests/test_engine_lifecycle.py::test_on_session_start_idempotent` — call twice, assert single row
- `tests/test_engine_lifecycle.py::test_on_session_start_initializes_last_seen` — assert `_last_seen_message_idx[session_id] == 0`
- `tests/test_engine_lifecycle.py::test_on_session_end_clears_state` — populate state, call end, assert cleared
- `tests/test_engine_lifecycle.py::test_on_session_reset_zeroes_tokens` — call reset; assert `last_prompt_tokens == 0`
- `tests/test_engine_lifecycle.py::test_ignored_session_skipped` — pass `ignore_session_patterns=["^test-.*"]`; assert no conversation row
- `tests/test_engine_lifecycle.py::test_no_jsonl_read` — instantiate engine in a tmp dir with no JSONL files; assert `on_session_start` doesn't try to read any file outside of `lcm.db` (mock `open`)

## Estimated effort
8 hours

## Confidence
90% — the lifecycle methods are straightforward, but a small ambiguity remains around `on_session_reset` (the ABC method has no `session_id` arg, but the OpenClaw `handleBeforeReset` had one). The plugin-glue hook (issue 02-07) registers `on_session_reset` separately with `session_id` kwargs — this issue's ABC override is the fallback for direct calls (e.g., `AIAgent.reset_session()` at `run_agent.py:2563`).
