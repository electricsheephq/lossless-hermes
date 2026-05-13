---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-02] plugin: register /lcm slash command with subcommand dispatcher (status + help only)'
labels: 'port, epic-02'
---

## Source (TypeScript)
- File: `src/plugin/lcm-command.ts`
- Lines: 2884 LOC total — Epic 02 only ports the dispatch skeleton + `status` + `help`
- Function(s)/class(es): `createLcmCommand`, `parseLcmCommand`, the switch dispatcher, and the `status` / `help` subcommand handlers

## Target (Python)
- File: `src/lossless_hermes/commands/__init__.py` (dispatcher) + `src/lossless_hermes/commands/status.py` + `src/lossless_hermes/commands/help.py`
- Estimated LOC: ~300 (dispatcher ~100, status ~120, help ~80)

## Summary

Register the `/lcm` slash command on `PluginContext` with a subcommand dispatcher. Per `docs/porting-guides/plugin-glue.md` "/lcm slash commands" section, there are ~13 subcommands eventually. **Epic 02 implements only 2 of them: `status` and `help`.** All others return `"subcommand <X> not yet implemented (Epic 08)"` so the surface is discoverable but the bodies don't block this epic.

Per ADR-013 (owner-gating): handlers do NOT check `is_owner` themselves. Gating is upstream via `gateway/slash_access.SlashAccessPolicy`. This issue's handlers are signed for `(raw_args: str) → str` and that's it.

Per `docs/reference/hermes-hooks.md` line 131: `register_command(name, handler, description="", args_hint="")`. The dispatcher handler is `lcm_command_dispatcher`. Both sync and async handlers are supported (line 168: "Async handlers are awaited by `resolve_plugin_command_result()` with a 30s timeout"). Epic 02 uses sync handlers; Epic 08 may switch to async where needed.

## Implementation

```python
# src/lossless_hermes/commands/__init__.py

import logging
import shlex
from typing import Callable, Optional

from hermes_cli.plugins import PluginContext
from ..config import LcmConfig
from ..shared_init import SharedLcmInit
from . import status as status_mod
from . import help as help_mod

logger = logging.getLogger("lcm.commands")


# Subcommand dispatch table. Keys are subcommand names; values are handlers
# (sync or async). Epic 08 fills in the unimplemented entries.
_DISPATCH_TABLE: dict[str, Callable[..., str]] = {
    # Implemented in Epic 02:
    "": status_mod.run,  # `/lcm` (no args) — alias for status
    "status": status_mod.run,
    "help": help_mod.run,
    # Epic 08 deliverables — stubs return "not yet implemented":
    "backup": None,
    "rotate": None,
    "health": None,
    "worker": None,
    "doctor": None,
    "reconcile-session-keys": None,
    "eval": None,
    "purge": None,
}


# Module-globals for handler closures. Set by register_lcm_command.
# Per plugin-glue.md "Per-subcommand translation table" — handlers receive
# only raw_args; engine state comes via closure over SharedLcmInit.
_SHARED: Optional[SharedLcmInit] = None
_CFG: Optional[LcmConfig] = None


def register_lcm_command(
    ctx: PluginContext,
    shared: SharedLcmInit,
    cfg: LcmConfig,
) -> None:
    """Register the /lcm slash command. Called from register() in __init__.py
    (issue 02-07).

    Maps to engine.ts:plugin/index.ts api.registerCommand(createLcmCommand(...)).
    """
    global _SHARED, _CFG
    _SHARED = shared
    _CFG = cfg

    ctx.register_command(
        name="lcm",
        handler=_lcm_command_dispatcher,
        description="LCM subsystem control (status, doctor, purge, ...)",
        args_hint="<subcommand>",
    )

    # Optional alias: /lossless points at the same dispatcher per plugin-glue.md.
    # OpenClaw canonical was `/lossless`; Hermes ports keep `/lcm` as canonical
    # but register the alias for OpenClaw muscle memory.
    ctx.register_command(
        name="lossless",
        handler=_lcm_command_dispatcher,
        description="Alias for /lcm (OpenClaw compat)",
        args_hint="<subcommand>",
    )


def _lcm_command_dispatcher(raw_args: str) -> str:
    """The entry point for /lcm <subcommand> [args].

    Per hermes-hooks.md: handler signature is `(raw_args: str) → str | None`.
    Per ADR-013: no in-handler owner check; gating is upstream via
    gateway/slash_access.
    """
    if _SHARED is None or _CFG is None:
        return "[lcm] command dispatcher not initialized — bug?"

    # Parse: first token is the subcommand; remainder is its args.
    tokens = shlex.split(raw_args or "")
    subcommand = tokens[0] if tokens else ""
    sub_args = " ".join(tokens[1:]) if len(tokens) > 1 else ""

    handler = _DISPATCH_TABLE.get(subcommand)

    if handler is None and subcommand in _DISPATCH_TABLE:
        # Known subcommand, not yet implemented
        return (
            f"/lcm {subcommand} is not yet implemented in this build. "
            f"Tracked under Epic 08 (CLI Ops). "
            f"Run /lcm help for the full inventory."
        )

    if handler is None:
        return (
            f"Unknown /lcm subcommand: {subcommand!r}. "
            f"Run /lcm help for valid subcommands."
        )

    try:
        return handler(sub_args, shared=_SHARED, cfg=_CFG)
    except Exception as exc:
        logger.exception("[lcm] dispatcher error in subcommand %r", subcommand)
        return f"/lcm {subcommand} failed: {exc!s}"
```

```python
# src/lossless_hermes/commands/status.py

from typing import Any
from ..shared_init import SharedLcmInit
from ..config import LcmConfig


def run(raw_args: str, *, shared: SharedLcmInit, cfg: LcmConfig) -> str:
    """`/lcm status` — full LCM health snapshot.

    Maps to engine.ts:plugin/lcm-command.ts case "status".
    Per Epic 02 README: returns "ok" or a minimal status block.

    For Epic 02 we ship a small JSON-shaped status block. Epic 08 grows this
    to the full status text per docs/porting-guides/plugin-glue.md line 426.
    """
    engine = shared.engine

    # Read-only fields; no DB write.
    return (
        f"[lcm] status\n"
        f"  engine: {engine.name}\n"
        f"  db: {engine.db_path}\n"
        f"  migrated: {engine.migrated}\n"
        f"  conversations: {engine.conversation_store.count_conversations()}\n"
        f"  context_threshold: {cfg.context_threshold}\n"
        f"  last_prompt_tokens: {engine.last_prompt_tokens}\n"
        f"  threshold_tokens: {engine.threshold_tokens}\n"
        f"  context_length: {engine.context_length}\n"
        f"  compression_count: {engine.compression_count}\n"
        f"  ok"
    )
```

```python
# src/lossless_hermes/commands/help.py

from ..shared_init import SharedLcmInit
from ..config import LcmConfig


# Tracks the ~13 subcommands per docs/porting-guides/plugin-glue.md.
_SUBCOMMAND_INVENTORY = [
    ("status", "(implemented in Epic 02)", "Full LCM health snapshot"),
    ("help", "(implemented in Epic 02)", "This message"),
    ("backup", "(Epic 08)", "VACUUM INTO a timestamped .bak file"),
    ("rotate", "(Epic 08, JSONL-dependent — may drop)", "Rotate session storage"),
    ("health", "(Epic 08)", "v4.1 health snapshot — workers + embeddings"),
    ("worker", "(Epic 08)", "Worker status; subcmd 'tick embedding-backfill' (owner)"),
    ("doctor", "(Epic 08)", "Read-only scan; 'doctor apply' (owner) re-summarizes"),
    ("doctor clean", "(Epic 08, owner)", "Listing/cleanup of high-confidence junk"),
    ("reconcile-session-keys", "(Epic 08, owner)", "List/apply session_key rewrites"),
    ("eval", "(Epic 09, owner)", "Eval harness against fts/semantic/hybrid"),
    ("purge", "(Epic 08, owner)", "Soft-suppress leaves + cascade"),
]


def run(raw_args: str, *, shared: SharedLcmInit, cfg: LcmConfig) -> str:
    """`/lcm help` — list available subcommands and their epic status.

    Maps to engine.ts:plugin/lcm-command.ts case "help".
    """
    lines = ["/lcm — Lossless Context Management commands", ""]
    for name, status, desc in _SUBCOMMAND_INVENTORY:
        lines.append(f"  /lcm {name:25s}  {status:38s}  {desc}")
    lines.append("")
    lines.append(
        "Owner-gating: destructive subcommands (purge, doctor apply, "
        "reconcile-session-keys, worker tick, eval, doctor clean) require "
        "the user to be in `allow_admin_from` per config.yaml. See ADR-013."
    )
    return "\n".join(lines)
```

## Dependencies
- Depends on: 02-01 (engine instance via shared init), 02-07 (called from `register(ctx)`); Epic 01 `ConversationStore.count_conversations()`
- Blocks: Epic 08 (fills in the unimplemented dispatch entries)

## Acceptance criteria
- [ ] `ctx.register_command("lcm", ...)` is invoked exactly once
- [ ] `ctx.register_command("lossless", ...)` is also invoked (alias)
- [ ] `_lcm_command_dispatcher("")` (no args) routes to `status` and returns a non-empty string containing `"ok"`
- [ ] `_lcm_command_dispatcher("status")` returns the same status block
- [ ] `_lcm_command_dispatcher("help")` returns a block listing all 11 subcommand entries from `_SUBCOMMAND_INVENTORY`
- [ ] `_lcm_command_dispatcher("backup")` returns `"... not yet implemented ... Epic 08 ..."`
- [ ] `_lcm_command_dispatcher("nonsense")` returns `"Unknown /lcm subcommand: 'nonsense' ..."`
- [ ] `_lcm_command_dispatcher("doctor apply")` parses 2 tokens correctly and returns the Epic-08 stub for `doctor` (the subcommand dispatch is single-level for Epic 02; nested `doctor apply` dispatch is Epic 08)
- [ ] Handler exceptions are caught and returned as `"/lcm <sub> failed: <exc>"` (don't crash the dispatcher)
- [ ] `pytest tests/test_commands_dispatcher.py` passes
- [ ] Manual integration: `echo "/lcm status" | hermes ...` in the Epic 02 README validation block returns the status text

## Tests
- `tests/test_commands_dispatcher.py::test_register_calls_register_command` — assert `ctx.register_command` called with `name="lcm"` (and the alias `"lossless"`)
- `tests/test_commands_dispatcher.py::test_no_args_aliases_status` — call dispatcher with `""`; assert response contains `engine: lcm` and `ok`
- `tests/test_commands_dispatcher.py::test_status_subcommand` — `dispatcher("status")` returns the same block
- `tests/test_commands_dispatcher.py::test_help_lists_subcommands` — `dispatcher("help")` returns a block with all entries from `_SUBCOMMAND_INVENTORY`
- `tests/test_commands_dispatcher.py::test_unimplemented_subcommand` — `dispatcher("purge")` returns the "Epic 08" stub
- `tests/test_commands_dispatcher.py::test_unknown_subcommand` — `dispatcher("foo")` returns the "Unknown" error
- `tests/test_commands_dispatcher.py::test_handler_exception_caught` — monkeypatch `status_mod.run` to raise; assert dispatcher returns `"/lcm status failed: ..."` and doesn't propagate
- `tests/test_commands_dispatcher.py::test_args_passed_to_handler` — `dispatcher("status --verbose")`; assert the handler receives `"--verbose"` as `sub_args` (Epic 02 status ignores them, but the wiring should be in place for Epic 08)
- `tests/test_commands_dispatcher.py::test_shlex_quoting` — `dispatcher('purge --reason "test with spaces"')`; assert the dispatcher routes to `purge` and the args preserve the quoted reason (smoke test for Epic 08's purge subcommand which needs `--reason "..."`)

## Estimated effort
12 hours

## Confidence
90% — the dispatcher is mechanical. The minor uncertainty is around the `/lossless` alias: per ADR-024 plugin manifest section and plugin-glue.md line 444, the alias is registered as a separate command pointing at the same handler. This is straightforward but the `register_command` call must accept name normalization (lowercased, hyphens for spaces, leading `/` stripped — see hermes-hooks.md line 131) consistently so both `/lcm` and `/lossless` route correctly.
