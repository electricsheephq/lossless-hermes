# Epic 02 — Engine Skeleton

Wire `LCMEngine` into Hermes's `ContextEngine` lifecycle. This is the first epic where the plugin actually loads, registers, and shows up as `context.engine: lcm` in a running Hermes session. No real compaction, no real assembly, no tool surface — just the load-bearing skeleton that every later epic plugs into.

## Goal

End-state, in one paragraph: Hermes starts with `context.engine: lcm` in `~/.hermes/config.yaml`. The `lossless-hermes` plugin loads via entry-point. `LCMEngine` instantiates against `$HERMES_HOME/lossless-hermes/lcm.db`, opens the DB from Epic 01, registers itself via `ctx.register_context_engine(engine)`. All 4 required Hermes hooks (`post_llm_call`, `pre_llm_call`, `on_session_end`, `subagent_stop`) register without errors. The `/lcm` slash command registers and responds to `/lcm status` (returns `"ok"`) and `/lcm help` (returns the inventory of subcommands that later epics will fill in). `compress(messages, ...)` is a no-op pass-through (real compaction lives in Epic 04). Per-session async locks are in place. Circuit-breaker state machine is scaffolded (auth-failure handling deferred to Epic 04). At the end of this epic, `pytest tests/test_engine_skeleton.py` passes and a manual `hermes` invocation with `context.engine: lcm` shows `Using context engine: lcm` in the startup banner.

## Deliverables

- **`LCMEngine` class** in `src/lossless_hermes/engine/__init__.py` (the shell class per ADR-027) — owns DB handle, sub-module stores from Epic 01, mixin composition, all required state fields.
- **All required `ContextEngine` ABC method overrides** wired but no-op or stub: `update_from_response`, `should_compress`, `compress` (no-op pass-through), `on_session_start`, `on_session_end`, `on_session_reset`.
- **All 4 Hermes hook registrations** in `register(ctx)`: `post_llm_call` (stub for Epic 03), `pre_llm_call` (returns the recall-policy prompt per ADR-014), `on_session_end` (lifecycle flush), `subagent_stop` (no-op stub for Epic 06).
- **Per-session async lock infrastructure** — `defaultdict[str, asyncio.Lock]` keyed by `conversation_id` per ADR-018.
- **Circuit-breaker state machine scaffold** — `dict[str, CircuitBreakerState]` with `failures: int`, `open_since: float | None`; transition methods (`open`, `record_failure`, `record_success`, `is_open`) ported from `engine.ts:1782, 1963-2016`. Real auth-failure handling lands in Epic 04 (depends on `summarize.py`).
- **Slash command dispatcher** — `ctx.register_command("lcm", lcm_dispatcher, args_hint="<subcommand>")`. `/lcm status` returns `"ok"` (or a small status block); `/lcm help` lists the planned subcommand inventory. Every other subcommand returns `"subcommand <X> not yet implemented (Epic 08)"`.
- **Smoke test** — `tests/test_engine_skeleton.py` instantiates `LCMEngine`, calls every required ABC method with mock args, asserts no exceptions and contract-shape correctness on returns. CI green for the epic.

## Dependencies

- **Epic 00 (Scaffolding)** — pyproject.toml + `pip install -e .` entry-point + `plugin.yaml` + repo layout. Required for the plugin to load at all.
- **Epic 01 (Storage)** — DB connection, migrations, the 4 stores (`ConversationStore`, `SummaryStore`, `CompactionTelemetryStore`, `CompactionMaintenanceStore`). The engine constructor calls into these directly.

## Blocks

- **Epic 03 (Ingest + Assembly)** — depends on the `_on_post_llm_call` hook stub from this epic to land its real diff-and-ingest body. Also depends on `pre_llm_call` hook registration for the assembly side.
- **Epic 04 (Compaction)** — depends on `compress()` skeleton, circuit-breaker scaffold, per-session locks, and `should_compress()` threshold logic from this epic. Replaces the no-op `compress()` body with the real compaction algorithm.
- **Epic 06 (Tools)** — depends on the engine surface (`get_tool_schemas`, `handle_tool_call`) being ready to accept tool registrations.
- **Epic 08 (CLI Ops)** — depends on the `/lcm` slash command dispatcher landing here. Adds the destructive subcommands (`/lcm purge`, `/lcm doctor apply`, etc.) on top.

## Critical path

**YES.** Without Engine Skeleton, no other epic can run end-to-end against a live Hermes. Epic 03 and Epic 04 both inject directly into the surface this epic defines.

## Estimated total effort

**~60–80 hours (2 weeks for one engineer).**

Breakdown:
- 02-01 engine `__init__`: 8 hours
- 02-02 state fields: 4 hours
- 02-03 lifecycle hooks: 8 hours
- 02-04 token tracking: 4 hours
- 02-05 should_compress: 6 hours
- 02-06 no-op compress: 4 hours
- 02-07 hook registrations: 8 hours
- 02-08 per-session locks: 6 hours
- 02-09 circuit-breaker scaffold: 8 hours
- 02-10 slash command dispatcher: 12 hours

Padding for integration debugging + smoke-test plumbing: 8–12 hours.

## Confidence

**90%.**

The 10% uncertainty is concentrated in two places:

1. **ADR-010 (always-on assembly) depends on a Hermes upstream PR** for `ContextEngine.preassemble(messages, budget) → messages`. Until that lands, the experimental fallback (`should_compress() → True` every turn, ADR-010 Option A) is documented as known-broken. This epic does NOT use the experimental fallback — it implements `should_compress()` as the conventional threshold gate. Always-on assembly substitution lands in Epic 03 once the preassemble hook or experimental flag is wired.
2. **Hermes version skew on `_EngineCollector.register_command`** (ADR-015 patch #2). The directory-mode loader silently drops `register_command` calls. lossless-hermes ships as an entry-point plugin (per ADR-024 / hermes-hooks.md), so this is sidestepped — but a contributor who tries to ship the plugin as a directory plugin will get a silent slash-command failure. Document this in the README and verify with a startup self-check.

Everything else (state fields, hooks, lock dict, circuit breaker scaffold) is mechanical 1:1 port work with no architectural ambiguity.

## Issues

| # | Title | Hours | Confidence | Deps |
|---|---|---|---|---|
| 02-01 | `LCMEngine.__init__` per ADR-024 / ADR-027 | 8 | 95% | Epic 00, 01 |
| 02-02 | All `LcmContextEngine` state fields | 4 | 95% | 02-01 |
| 02-03 | `on_session_start`/`on_session_end`/`on_session_reset` per ADR-011 | 8 | 90% | 02-01, 02-02 |
| 02-04 | `update_from_response(usage)` token tracking | 4 | 95% | 02-02 |
| 02-05 | `should_compress(prompt_tokens)` with anti-thrashing back-off | 6 | 90% | 02-04 |
| 02-06 | No-op `compress(messages, current_tokens, focus_topic)` | 4 | 95% | 02-01 |
| 02-07 | Hook registrations (`post_llm_call`, `pre_llm_call`, `on_session_end`, `subagent_stop`) | 8 | 90% | 02-01, 02-03 |
| 02-08 | Per-session async locks (`defaultdict[str, asyncio.Lock]`) per ADR-018 | 6 | 95% | 02-02 |
| 02-09 | Circuit-breaker state machine scaffold | 8 | 90% | 02-02 |
| 02-10 | `/lcm` slash command dispatcher (`status` + `help` only) | 12 | 90% | 02-01, 02-07 |

## Validation

At the end of this epic:

```bash
# Smoke test — engine instantiates and registers without errors
pytest tests/test_engine_skeleton.py -v

# Manual integration — launch Hermes with LCM selected
HERMES_HOME=/tmp/lcm-smoke hermes --config /tmp/test-config.yaml
# Expect banner: "Using context engine: lcm"
# Expect log line: "[lcm] Plugin loaded (db=..., threshold=0.75)"

# Slash command roundtrip
echo "/lcm status" | hermes --config /tmp/test-config.yaml
# Expect: "ok" (or status JSON with engine.name, db_path, conversation_count)
echo "/lcm help" | hermes --config /tmp/test-config.yaml
# Expect: list of subcommands with "Epic 08" marker on unimplemented ones
```

No real ingest, no real compaction, no real assembly. That's Epic 03 and 04. This epic proves the wiring is sound.
