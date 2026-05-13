---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: full /lcm subcommand dispatch table'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/plugin/lcm-command.ts`
- Lines: 2884 LOC (dispatcher body is ~200 LOC; the rest is per-subcommand handler code split out into 08-02..08-17)
- Function(s)/class(es): `createLcmCommand(...)`, `parseLcmCommand(args: string)`, the giant `switch (subcommand)` block (per plugin-glue.md §"/lcm slash commands — full inventory"), the `__testing` export surface

## Target (Python)

- File: `src/lossless_hermes/plugin/commands.py`
- Estimated LOC: ~280 (Hermes is leaner — owner-gating is upstream per ADR-013, so the dispatcher routes `raw_args` directly without `senderIsOwner` ceremony)

## What this issue covers

Replace the Epic 02 single-handler scaffold with the full `/lcm` subcommand dispatch table. This issue ships **the dispatcher + token splitter + alias registration only**; per-subcommand handler bodies land in 08-02..08-17 and are imported into the dispatcher table.

1. **Token splitter** (`parse_lcm_command(raw_args: str) -> ParsedLcmCommand`). Ports the TS shell-style argument splitter that honors `--reason "..."` quoting, `--from a,b,c` comma-lists, and bare flags like `--apply` / `--allow-main-session` / `--baseline` / `--vacuum`. Pure function over the input string; no DB access. Test against the TS fixture (`test/lcm-command.test.ts:__testing.parseLcmCommand`).

2. **Dispatch table** — a single `dict[str, Callable[[ParsedLcmCommand], str | None]]` keyed by the canonical subcommand name. The 17 entries (per plugin-glue.md "/lcm slash commands — full inventory" table):

   | Key | Handler module | Owner-gated (per ADR-013) |
   |---|---|---|
   | `""` (no args) → alias `status` | `commands.status:run` | no |
   | `status` | `commands.status:run` | no |
   | `backup` | `commands.backup:run` | no |
   | `rotate` | `commands.rotate:run` | no |
   | `health` | `commands.health:run` | no |
   | `worker` / `worker status` | `commands.worker:run_status` | no |
   | `worker tick embedding-backfill` | `commands.worker:run_tick_backfill` | YES |
   | `doctor` (no args, or `doctor scan`) | `commands.doctor:run_scan` | no |
   | `doctor apply` | `commands.doctor:run_apply` | YES |
   | `doctor clean` (read-only listing) | `commands.doctor:run_cleaners_scan` | YES (Wave-12 P1 fix) |
   | `doctor clean apply [filter-id] [vacuum]` | `commands.doctor:run_cleaners_apply` | YES |
   | `reconcile-session-keys --list-candidates` | `commands.reconcile:run_list` | YES |
   | `reconcile-session-keys --apply ...` | `commands.reconcile:run_apply` | YES |
   | `eval ...` | `commands.eval:run` | YES |
   | `purge ...` | `commands.purge:run` | YES |
   | `import-openclaw ...` | `cli.import_openclaw:run_slash` | YES |
   | `help` | `commands.help:run` | no |

3. **Help text** — `commands.help:run` returns a multi-line help string listing all 17 subcommands grouped by destructiveness, with one-line descriptions and a footer pointing at `~/.hermes/config.yaml`'s `allow_admin_from` block. No-args fall-through to `status` (not `help`) for TS parity.

4. **Alias registration** — register `/lcm` as the canonical command and `/lossless` as a separate `ctx.register_command("lossless", ...)` pointing at the same handler closure. Both names show up in `hermes plugins list` and the Telegram menu (per plugin-glue.md §"Hidden Telegram surface"; Hermes can't hide one alias from the menu unless `gateway/telegram_bot.py:telegram_bot_commands` is patched, which is out of scope).

5. **Owner-gating is NOT checked here.** Per ADR-013, the gateway upstream of dispatch enforces `allow_admin_from`. The dispatcher trusts that `raw_args` arriving at the handler means the caller is authorized. This is a deliberate departure from the TS `senderIsOwner` per-subcommand check.

6. **Startup warning** — at register time, if `cfg.is_gateway_mode and not slash_access.allow_admin_from`, log a single WARNING-level banner: `[lcm] WARNING: destructive /lcm subcommands are exposed to all users — set allow_admin_from in ~/.hermes/config.yaml to restrict.` (Per ADR-013 Consequences and the recommended mitigation in plugin-glue.md §"Remaining 5% risk" #4.)

7. **`/lcm` alias for `/lcm status`** — bare `/lcm` with no args routes to `commands.status:run` (TS parity; doctor-ops.md §"Operator gate" — operators expect `/lcm` to be a status query).

## Dependencies

- Depends on: Epic 02 (engine skeleton — `LcmContextEngine` instance + `current_session_id` accessor + `ctx.register_command` wired) — must be merged first.
- Blocks: every other issue in Epic 08 (08-02 through 08-17 register their handlers into this dispatch table).

## Acceptance criteria

- [ ] `parse_lcm_command(raw_args)` honors `--reason "quoted value"`, `--from a,b,c`, bare flags, and unknown flags (raises `LcmCommandParseError` with the offending token).
- [ ] All 17 dispatch keys are routed to the correct handler module (verified by mocking each handler to return a unique sentinel string and asserting the dispatcher returns it).
- [ ] `/lcm` (bare) returns the same output as `/lcm status`.
- [ ] `/lcm help` lists all 17 subcommands; owner-gated ones are marked with `(admin)` suffix.
- [ ] `/lossless` alias is registered as a separate command pointing at the same handler closure.
- [ ] The "destructive subcommands need `allow_admin_from`" startup warning fires when `is_gateway_mode and not allow_admin_from`.
- [ ] No per-subcommand `is_owner` check in the dispatcher (ADR-013 invariant — `grep -n "is_owner" src/lossless_hermes/plugin/commands.py` returns 0 lines).
- [ ] All 8 TS dispatcher-table tests in `test/lcm-command.test.ts:__testing` have ported pytest equivalents in `tests/commands/test_dispatcher.py`.
- [ ] New test: `tests/commands/test_parse_lcm_command.py` — token splitter quoting/list/flag invariants per plugin-glue.md §"Test inventory" line 590.
- [ ] New test: `tests/commands/test_owner_gating.py` — mock `SlashAccessPolicy.deny()` and assert the handler body is never reached for destructive subcommands (ADR-013 §"Open questions" line 90).
- [ ] Function signatures match the spec in [docs/porting-guides/plugin-glue.md](../../docs/porting-guides/plugin-glue.md) §"/lcm slash commands — full inventory".
- [ ] `pytest tests/commands/test_dispatcher.py tests/commands/test_parse_lcm_command.py tests/commands/test_owner_gating.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/plugin/commands.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**6 hours.**

## Confidence

**95%** — the dispatch table is mechanical translation work; the TS source is fully enumerated in plugin-glue.md. The only uncertainty is in the alias registration (Hermes's `register_command` doesn't accept aliases natively per plugin-glue.md line 446); a second-command registration is the documented workaround.
