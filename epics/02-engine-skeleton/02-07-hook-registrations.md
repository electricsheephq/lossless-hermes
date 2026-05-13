---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] plugin: register all Hermes hooks (post_llm_call, pre_llm_call, on_session_end, subagent_stop)'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/plugin/index.ts`
- Lines: ~280–420 (`wirePluginHandlers` function — the four lifecycle hook registrations: `llm_output`, `before_reset`, `before_prompt_build`, `session_end`, plus `gateway_stop`/`gateway_start`)
- Function(s)/class(es): `wirePluginHandlers`

## Target (Python)
- File: `src/lossless_hermes/__init__.py` (the entry-point `register(ctx)` function)
- Estimated LOC: ~120

## Summary

In `register(ctx: PluginContext)`, register all required Hermes hooks per `docs/reference/hermes-hooks.md` "Where LCM hooks land" table (lines 322–334):

| Hermes hook | LCM purpose | ADR |
|---|---|---|
| `post_llm_call` | Per-turn ingest (diff `conversation_history` against `_last_seen_message_idx`) | ADR-009 |
| `pre_llm_call` | Inject `LOSSLESS_RECALL_POLICY_PROMPT` into user message | ADR-014 |
| `on_session_end` | Final-snapshot defense-in-depth for interrupted turns (ADR-009 Consequences) | ADR-009 |
| `subagent_stop` | No-op stub for v2 (ADR-012 defers subagent context-sharing) | ADR-012 |

This issue is the **glue** between Hermes's `PluginContext` and the engine's lifecycle methods. The hook bodies are mostly **stubs that delegate** to engine methods (which other Epic 02 issues implement). The real diff-and-ingest body lives in Epic 03; this issue ships the wiring.

## Hook registration sequence

Per `docs/porting-guides/plugin-glue.md` lines 343–391 — the recommended `_wire_handlers` order, adapted for Epic 02:

```python
# src/lossless_hermes/__init__.py

import logging
from typing import Any
from hermes_cli.plugins import PluginContext
from hermes_cli.config import load_config, cfg_get
from hermes_constants import get_hermes_home

from .engine import LCMEngine
from .commands import lcm_command_dispatcher  # issue 02-10
from .config import LcmConfig
from .shared_init import get_shared_init, set_shared_init, normalize_db_path, SharedLcmInit

LOGGER = logging.getLogger(__name__)

# Imported from a constants module per plugin-glue.md — see ADR-014 for
# the reword required when injecting into user-message position vs system.
LOSSLESS_RECALL_POLICY_PROMPT = """When working with this conversation, the
lossless-hermes plugin provides DAG-based compaction and recall tools.

For compacted conversation history, these instructions supersede generic
memory-recall guidance.
... (full reworded prompt — see docs/recall-policy-prompt.md once written)
"""


def register(ctx: PluginContext) -> None:
    """Hermes plugin entry per ADR-001. Called once per process at startup."""
    raw_config = cfg_get(load_config(), "plugins", "entries", "lossless-hermes", default={})
    cfg = LcmConfig.model_validate(raw_config) if raw_config else LcmConfig()

    if not cfg.enabled:
        LOGGER.info("[lcm] disabled via config")
        return

    db_path = normalize_db_path(cfg.database_path or f"{get_hermes_home()}/lossless-hermes/lcm.db")

    # Singleton check (per plugin-glue.md). On a re-register (discover_plugins(force=True)),
    # reuse the existing engine.
    existing = get_shared_init(db_path)
    if existing and not existing.stopped:
        LOGGER.info("[lcm] reusing shared engine init for db=%s", db_path)
        _wire_handlers(ctx, existing, cfg)
        return

    # Fresh init
    engine = LCMEngine(hermes_home=get_hermes_home(), config=cfg)
    shared = SharedLcmInit(engine=engine, stopped=False)
    set_shared_init(db_path, shared)

    _wire_handlers(ctx, shared, cfg)

    LOGGER.info(
        "[lcm] Plugin loaded (db=%s, threshold=%.2f)",
        db_path, cfg.context_threshold,
    )


def _wire_handlers(ctx: PluginContext, shared: SharedLcmInit, cfg: LcmConfig) -> None:
    """Register context engine + 4 hooks + slash command."""
    engine = shared.engine

    # 1. Context engine slot — must be registered for context.engine: lcm to select it.
    ctx.register_context_engine(engine)

    # 2. post_llm_call: per-turn ingest (ADR-009). Stub for Epic 02 — the real
    # diff-and-ingest body lands in Epic 03.
    async def _on_post_llm_call(
        *,
        session_id: str,
        user_message: Any = None,
        assistant_response: str = "",
        conversation_history: list = None,
        model: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> None:
        """Hermes post_llm_call fires once per turn at run_agent.py:15410,
        AFTER transform_llm_output, only when final_response and not interrupted.

        Maps to engine.ts:afterTurn (6220-6646). Epic 02: stub that logs.
        Epic 03: diff conversation_history against engine._last_seen_message_idx
        and ingest the delta.
        """
        if conversation_history is None:
            return
        logger.debug(
            "[lcm] post_llm_call session=%s history_len=%d (Epic 03 will diff and ingest)",
            session_id, len(conversation_history),
        )
        # Epic 03: await engine._on_post_llm_call_impl(...)

    ctx.register_hook("post_llm_call", _on_post_llm_call)

    # 3. pre_llm_call: recall policy injection (ADR-014). User-message position
    # to preserve prompt cache (NOT system prompt).
    async def _on_pre_llm_call(
        *,
        session_id: str,
        user_message: Any = None,
        conversation_history: list = None,
        is_first_turn: bool = False,
        model: str = "",
        platform: str = "",
        sender_id: str = "",
        **kwargs: Any,
    ) -> dict:
        """Hermes pre_llm_call fires once per user turn at run_agent.py:12034,
        before the tool-calling loop. Plugins return {"context": "..."} or a
        plain string; result is APPENDED to the current turn's user message.

        Per ADR-014: inject the LOSSLESS_RECALL_POLICY_PROMPT into user position
        (not system) to preserve Anthropic prompt cache.

        Epic 02: returns the (reworded) policy text. Epic 03 may extend to
        inject assembled context items per ADR-010 if always-on assembly
        ships via the experimental fallback.
        """
        return {"context": LOSSLESS_RECALL_POLICY_PROMPT}

    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    # 4. on_session_end: per-turn fire (NOT real session boundary). Defense-in-depth
    # for interrupted turns (ADR-009 Consequences).
    # NOTE: The ContextEngine ABC has its own on_session_end which fires at REAL
    # session boundaries (run_agent.py:5575,5600). This plugin-hook on_session_end
    # fires at EVERY turn end (run_agent.py:15525). Both have a role:
    #   - The ABC method handles state flush at real boundaries.
    #   - The plugin hook handles defense-in-depth catch-up on interrupted turns.
    def _on_session_end_hook(
        *,
        session_id: str,
        completed: bool = True,
        interrupted: bool = False,
        model: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> None:
        """Hermes on_session_end (plugin hook) fires at end of EVERY run_conversation
        (run_agent.py:15525). Distinct from ContextEngine.on_session_end (run_agent.py:5575).

        Epic 02: stub. Epic 03: catch up on tail messages if interrupted=True
        and post_llm_call didn't fire (run_agent.py:15407 gates on final_response
        and not interrupted).
        """
        if interrupted:
            logger.debug(
                "[lcm] interrupted turn — Epic 03 will catch up on tail (session=%s)",
                session_id,
            )

    ctx.register_hook("on_session_end", _on_session_end_hook)

    # 5. subagent_stop: no-op for Epic 02 / v1 per ADR-012 (subagent context
    # sharing deferred to v2).
    def _on_subagent_stop(
        *,
        parent_session_id: str,
        child_role: Any = None,
        child_summary: str = "",
        child_status: str = "",
        duration_ms: int = 0,
        **kwargs: Any,
    ) -> None:
        """Hermes subagent_stop fires once per child after delegate_task runs
        (tools/delegate_tool.py:2248). Per ADR-012, v1 has no subagent context
        sharing — this hook is registered but no-op."""
        logger.debug(
            "[lcm] subagent_stop parent=%s status=%s (v1: no-op per ADR-012)",
            parent_session_id, child_status,
        )

    ctx.register_hook("subagent_stop", _on_subagent_stop)

    # 6. Slash command (issue 02-10)
    from .commands import register_lcm_command
    register_lcm_command(ctx, shared, cfg)
```

## Dependencies
- Depends on: 02-01 (engine class), 02-02 (state fields), 02-03 (lifecycle ABC methods), 02-10 (slash command — registered last in the wire sequence; can be stubbed if 02-10 lands later)
- Blocks: Epic 03 (replaces the `post_llm_call` stub body with real diff-and-ingest); Epic 04 (similar for compaction-decision hook)

## Acceptance criteria
- [ ] `register(ctx)` runs without errors when called with a mock `PluginContext`
- [ ] `ctx.register_context_engine` is called exactly once with an `LCMEngine` instance
- [ ] `ctx.register_hook` is called for each of: `post_llm_call`, `pre_llm_call`, `on_session_end`, `subagent_stop`
- [ ] `ctx.register_command` is called for `lcm`
- [ ] `pre_llm_call` hook returns `{"context": <policy text>}` with non-empty text
- [ ] `post_llm_call` hook accepts the kwargs shape per hermes-hooks.md (`session_id, user_message, assistant_response, conversation_history, model, platform`) — doesn't raise on any
- [ ] `subagent_stop` hook accepts its kwargs per hermes-hooks.md (`parent_session_id, child_role, child_summary, child_status, duration_ms`)
- [ ] Repeated `register(ctx)` (singleton path) does NOT open a second DB handle
- [ ] `pytest tests/test_register_hooks.py` passes

## Tests
- `tests/test_register_hooks.py::test_register_calls_all_expected_methods` — mock `PluginContext`; assert `register_context_engine`, `register_hook` (×4), `register_command` all called
- `tests/test_register_hooks.py::test_pre_llm_call_returns_policy` — capture the registered `pre_llm_call` callable; invoke it; assert return is `{"context": <non-empty str>}`
- `tests/test_register_hooks.py::test_post_llm_call_accepts_all_kwargs` — invoke the registered callable with a full kwargs dict matching hermes-hooks.md line 92; assert no raise
- `tests/test_register_hooks.py::test_on_session_end_accepts_interrupted` — invoke with `interrupted=True`; assert no raise and a debug log is emitted
- `tests/test_register_hooks.py::test_subagent_stop_noop` — invoke; assert no raise (no-op contract for v1)
- `tests/test_register_hooks.py::test_singleton_reuse` — call `register(ctx)` twice with the same DB path; assert only one `LCMEngine` instance was created (check via `mock.LCMEngine.call_count == 1` after the second call goes through the singleton path)
- `tests/test_register_hooks.py::test_disabled_skips_registration` — pass config with `enabled: false`; assert `register_context_engine` is NOT called

## Estimated effort
8 hours

## Confidence
90% — the hook signatures are precisely documented in `hermes-hooks.md` (the VALID_HOOKS table). The only ambiguity is whether `on_session_end` (plugin hook) and `ContextEngine.on_session_end` (ABC method) should both fire on the same event — per hermes-hooks.md they're distinct (plugin hook fires per-turn, ABC method fires at real boundaries). This issue keeps them separate with distinct logging messages so the integration test can tell which fired.
