---
name: Port issue
about: Wire always-on assembly per ADR-010 — preassemble (preferred) + force-compress fallback
title: '[epic-03] engine: wire always-on substitution per ADR-010'
labels: 'port'
---

## Source (TypeScript)
- File: `src/engine.ts` (`pr-613` HEAD `1f07fbd`)
- Lines: 6648–6832 (`assemble` — the OpenClaw entry point that called `engine.assemble()` BEFORE every turn). The substitution semantic.
- Function(s)/class(es): `engine.assemble` semantic; in Python this becomes the seam wired into Hermes's call path.

Hermes-side seam (read for context, not ported as-is):
- `agent/context_engine.py` ABC — `compress` (sync, threshold-gated), `should_compress` (sync predicate), `should_compress_preflight` (declared but DEAD CODE on hot path per spike 002).
- Hypothetical upstream addition: `preassemble(messages, budget_tokens) -> messages` (ADR-015 patch #1 / ADR-010 Option B).
- `run_agent.py:10264` — `messages = self.context_compressor.compress(messages, ...)` replaces the live list.
- `run_agent.py:12018` (hypothetical post-patch) — `messages = self.context_compressor.preassemble(messages, ...)`.

## Target (Python)
- File: `src/lossless_hermes/engine/assemble.py` (`_AssembleMixin`) — add `preassemble(...)` method + `compress(...)` override that branches on the experimental flag.
- File: `src/lossless_hermes/engine/__init__.py` — minor: detect `preassemble` ABC presence at engine init and log which mode is active.
- Estimated LOC: ~150 (two entry methods + the config gate + logging).

## Background

Per **ADR-010** (status: Proposed, 60% confidence):

> Chosen: Option B (upstream PR for `preassemble`), with Option A as documented experimental fallback.

The substitution architecture has two paths:

### Path 1 (preferred) — `preassemble` ABC patch is merged upstream

When the running Hermes has `ContextEngine.preassemble` in the ABC, the call site at `run_agent.py:12018` (post-patch) invokes `messages = self.context_compressor.preassemble(messages, budget_tokens=...)` BEFORE `pre_llm_call`, AFTER preflight compression. LCM's `preassemble` rewrites the message list every turn via `_AssembleMixin._assemble(...)`. The system prompt stays stable (cache preserved). The session does NOT rotate. Memory provider lineage stays intact.

### Path 2 (experimental fallback) — `preassemble` ABC patch NOT yet merged

When the running Hermes does NOT have `preassemble`, fall back to forcing `should_compress() == True` every turn. This routes through `compress(messages, ...)` whose return value replaces the live list at `run_agent.py:10264`. The substitution algorithm is the same; the side effects from `_compress_context` are real and documented:

- Session-ID rotates every turn (`run_agent.py:10311–10337`).
- `commit_memory_session(messages)` fires every turn — memory providers re-extract.
- Compression-count warning trips in 2–3 turns.
- File-read dedup cache reset every turn.
- Log spam.

**This path is gated behind** `plugins.entries.lossless-hermes.experimental.always_on_via_compress: true` in config. Default OFF. README + release notes must explicitly call out the breakage.

## Detection at engine init

```python
# src/lossless_hermes/engine/__init__.py (additions to __init__)
def __init__(self, hermes_home, config=None, summarizer=None):
    super().__init__()
    # ... existing init ...
    self._has_preassemble = hasattr(super(), "preassemble") or "preassemble" in dir(ContextEngine)
    self._experimental_always_on_via_compress = (
        config.get("experimental", {}).get("always_on_via_compress", False)
        if config else False
    )
    if not self._has_preassemble and not self._experimental_always_on_via_compress:
        logger.warning(
            "lossless-hermes: always-on substitution disabled. "
            "Upstream Hermes lacks `preassemble` ABC method (ADR-010 patch pending), "
            "and `experimental.always_on_via_compress` is False. "
            "Plugin will function as overflow-compactor only — no per-turn DAG substitution."
        )
    elif self._has_preassemble:
        logger.info("lossless-hermes: always-on substitution via preassemble (production mode).")
    else:
        logger.warning(
            "lossless-hermes: EXPERIMENTAL always-on substitution via force-compress. "
            "Session ID will rotate every turn; memory provider lineage breaks. NOT FOR PRODUCTION."
        )
```

## Method implementations

### `preassemble` (Path 1)

```python
def preassemble(
    self,
    messages: list[dict],
    budget_tokens: int | None = None,
) -> list[dict]:
    """Maps to engine.ts:6648–6832 (assemble) via ADR-010 Option B.

    Called BEFORE pre_llm_call, AFTER preflight compression. Rewrites the
    message list from the DAG under a token budget.
    """
    session_id = self._infer_session_id(messages)
    if not session_id:
        return messages  # graceful no-op
    return asyncio.run(self._assemble(
        session_id=session_id,
        messages=messages,
        token_budget=budget_tokens or self.context_length,
    ))
```

### `should_compress` (Path 2 gate)

```python
def should_compress(self, prompt_tokens: int = None) -> bool:
    if self._experimental_always_on_via_compress and not self._has_preassemble:
        # Force compress every turn → substitution via compress() path.
        return True
    # Default: overflow-only behavior. compress() is recovery, not substitution.
    observed = prompt_tokens or self.last_prompt_tokens
    return observed >= self.threshold_tokens
```

### `compress` (Path 2 substitution body + Path 1 overflow-recovery body)

```python
def compress(
    self,
    messages: list[dict],
    current_tokens: int = None,
    focus_topic: str = None,
) -> list[dict]:
    session_id = self._infer_session_id(messages)
    if not session_id:
        return messages

    if self._experimental_always_on_via_compress and not self._has_preassemble:
        # Substitution-via-compress path.
        return asyncio.run(self._assemble(
            session_id=session_id,
            messages=messages,
            token_budget=self.context_length,
            prompt=focus_topic,
        ))

    # Overflow-recovery path: compact-then-assemble (Epic 04 wires the compaction step).
    return asyncio.run(self._compress_async(session_id, messages, current_tokens, focus_topic))
```

(`_compress_async` is Epic 04 territory — calls `_execute_compaction_core` then `_assemble`. This issue stubs it as `return self._assemble(...)` only; Epic 04 fills in the compaction body.)

### `_infer_session_id`

Hermes does not pass `session_id` to `compress` — it must be inferred. Options:

- Cache it from the most recent `_on_post_llm_call(session_id=...)` fire on the same engine instance.
- Read it from the `messages` list metadata if Hermes encodes it there (verify by reading `agent/context_compressor.py` call site).
- Default to a "single-session" fallback that uses a sentinel id (works for tests; production must hit one of the above).

Document the choice + fallback in the code comment.

## Cache-hit measurement (open question from ADR-010)

> `pre_llm_call` rewrites the entire message list — the cache impact for `preassemble` is unmeasured. Run cache-hit benchmark in Phase 2 once the patch lands.

Wire telemetry: every call to `preassemble` / `compress`-substitution records the input vs output message-list hash. A subsequent Phase 2 PR adds the cache-hit measurement (out of scope here).

## Dependencies
- Depends on: #03-08 (orchestration is the substitution body), #03-02 + #03-03 (need ingest to land rows for the current turn before substitution fires next turn).
- Blocks: Epic 04 (compaction wires `_compress_async`'s compaction step into the seam this issue defines).

## Acceptance criteria

- [ ] Engine init detects whether Hermes has `preassemble` ABC method and logs the active mode.
- [ ] `experimental.always_on_via_compress` config flag is respected; default is `False`.
- [ ] When `preassemble` ABC exists, override is called every turn and `messages` is replaced by `_assemble` output.
- [ ] When `preassemble` ABC missing AND `experimental.always_on_via_compress=True`: `should_compress` returns `True` every turn; `compress` body runs `_assemble` and returns the replacement list.
- [ ] When `preassemble` ABC missing AND `experimental.always_on_via_compress=False`: `should_compress` returns `True` only on threshold breach; `compress` body runs the overflow-recovery path (compact-then-assemble; compaction step stubbed for Epic 04).
- [ ] Logs at startup state which mode is active.
- [ ] When experimental mode is active, an explicit warning log fires at engine init AND on every turn (rate-limited to once per minute).
- [ ] `_infer_session_id` covers the documented options and falls back gracefully.
- [ ] Sync→async bridge inside `compress` works without "loop already running" errors when Hermes calls it (mirror the pattern from #03-03).
- [ ] Function signatures match `docs/porting-guides/engine.md` §"assemble(params)" + ADR-010 §"Patches to propose" patch #1 sketch.
- [ ] Smoke test mounts the engine with a stub Hermes that has `preassemble` and asserts substitution happens every turn.
- [ ] Smoke test mounts the engine with a stub Hermes WITHOUT `preassemble` and `experimental.always_on_via_compress=True` and asserts `compress` is called every turn.
- [ ] Smoke test asserts the deprecation/experimental warning is emitted in fallback mode.
- [ ] `pytest tests/test_engine_substitution.py` passes locally + on GitHub CI.
- [ ] No new mypy errors.
- [ ] PR description cites the LCM commit SHA being ported + ADR-010 reference.

## Tests

- Production mode (preassemble exists): substitution fires every turn; system prompt unchanged.
- Experimental mode (force-compress): substitution fires every turn; warning emitted.
- Disabled mode (neither path active): `compress` only fires on threshold breach; `should_compress` defaults to threshold logic.
- Session-id inference: from cached recent session, from message metadata, fallback sentinel — each path covered.
- Mode switch at runtime: changing the experimental flag and reloading config switches the mode (or document that it requires restart).

## Estimated effort
**12 hours**. The complexity is in the dual-path detection + the sync→async bridge inside `compress`. The substitution body itself is just `_assemble`.

## Confidence
**60%**. The mechanism is settled (ADR-010), but the upstream PR is not yet merged. v0.1.0 must ship with the experimental fallback documented and tested; that path has real side effects that may surface in user-facing ways (compression-count warnings, log spam). If the upstream patch slips past v0.1.0 ship, the README/release notes carry the load. Mitigation: file the upstream PR early in Phase 2 per ADR-015.
