# ADR-013: Owner-gating mechanism

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 90%
**Supersedes:** —
**Superseded by:** —

## Context

OpenClaw LCM exposes ~13 `/lcm` subcommands. **9 of them are destructive or operator-only** (per `docs/porting-guides/plugin-glue.md` "Owner-gating count: 9 out of 13"):

- `/lcm worker tick embedding-backfill` (burns paid Voyage quota)
- `/lcm doctor apply` (re-summarizes via LLM, costs tokens)
- `/lcm doctor clean`, `/lcm doctor clean apply [filter-id] [vacuum]` (DELETEs rows; reveals session_key + previews across all conversations)
- `/lcm reconcile-session-keys --list-candidates`, `--apply` (rewrites session_key on conversations + summaries)
- `/lcm eval [--baseline] [...]` (paid embedding cost in hybrid mode)
- `/lcm purge --reason "..." [...]` (soft-suppresses leaves + cascade)

In OpenClaw, every `OpenClawPluginCommandDefinition.handler` received `ctx: PluginCommandContext` containing `ctx.senderIsOwner: boolean`. The plugin checked this inside each subcommand case and returned an "operator-only" rejection text. **The gate lived inside the plugin handler.**

In Hermes, the model is fundamentally different. Hermes's `register_command(name, handler, ...)` signature passes only `handler(raw_args: str) → str | None` — there is **no per-call context object**. Owner-gating is enforced **before** the handler runs, by `gateway/slash_access.SlashAccessPolicy` (`gateway/slash_access.py`).

Specifically (per `docs/porting-guides/plugin-glue.md` "Owner-gating in Hermes" section, lines 451–490):

- The gateway layer computes `policy = policy_for_source(self.config, source)` from `allow_admin_from` in `config.yaml` (`gateway/run.py:8270`).
- Non-admin users get `"⛔ /lcm is admin-only here. ..."` and the handler is never called.
- The handler runs ONLY when the user is authorized.

The constraint forcing a choice: how should lossless-hermes structure owner-gating for its ~9 destructive `/lcm` subcommands when the Hermes-native gate lives upstream of the handler?

## Options considered

### Option A: Pure upstream gate via `gateway/slash_access.SlashAccessPolicy`

- **Description:** Don't re-implement `senderIsOwner` checks inside the LCM command dispatcher. Document the operator requirement: "Operators MUST set `allow_admin_from` in `config.yaml` for every platform that runs lossless-hermes." Trust Hermes's upstream gate.
- **Pros:** Smallest surface. Idiomatic — uses the Hermes-native mechanism. Aligns with other Hermes plugins. Declarative gating in config.yaml.
- **Cons:** A misconfigured `allow_admin_from` (empty / unset) leaves destructive commands open to any DM-allowed user. Defense-in-depth absent.
- **Evidence:** `docs/porting-guides/plugin-glue.md:484` ("The cleanest path: rely on gateway/slash_access.py for the primary gate, and document the requirement that operators set allow_admin_from for every platform that runs LCM.").

### Option B: Request a `request_context` thread-local in Hermes core

- **Description:** Open an upstream Hermes PR adding a thread-local `request_context` carrying `is_owner`, `source.user_id`, etc. Have LCM handlers do per-subcommand `if not request_context.is_owner: return rejection_text` checks (mirroring the TS pattern).
- **Pros:** Defense-in-depth. Plugin-level enforcement is portable across platform configurations.
- **Cons:** Requires upstream Hermes change. Couples plugin to Hermes-internal thread-local API. Per-subcommand boilerplate. Doesn't add value if `allow_admin_from` is configured correctly.
- **Evidence:** `docs/porting-guides/plugin-glue.md:487` ("Option B — request a request_context thread-local in Hermes core, then add per-subcommand if not request_context.is_owner: return rejection_text checks").

### Option C: Split destructive subcommands into separate slash commands

- **Description:** Register `/lcm-purge`, `/lcm-doctor-apply`, `/lcm-reconcile`, etc. as separate top-level slash commands. Operators can put them in different `allow_admin_from` / `user_allowed_commands` tiers.
- **Pros:** Operators can fine-tune per-command gating without modifying handler code.
- **Cons:** Pollutes the command namespace. Loses the `/lcm <subcommand>` UX. Higher surface area on Hermes's command registry.
- **Evidence:** `docs/porting-guides/plugin-glue.md:488` ("Option C — register destructive subcommands as separate slash commands").

## Decision

Chosen: **Option A — pure upstream gate via `gateway/slash_access.SlashAccessPolicy`**

LCM `/lcm` subcommand handlers do NOT check `is_owner` themselves. Owner-gating is declarative, configured by the operator in `~/.hermes/config.yaml`'s `allow_admin_from` block. Hermes enforces the gate BEFORE the handler runs. The handler only receives `raw_args: str`.

## Rationale

Plugin-glue.md identifies this as the largest behavioral divergence from OpenClaw (line 452: "Owner-gating is **upstream of the handler**. The gateway layer ... rejects the command BEFORE dispatching to the plugin handler.").

The Hermes contract is intentional and well-designed:

- Gating is declarative (config.yaml), not imperative (per-handler check).
- The handler receives only what it needs to do its job (`raw_args`). No security-relevant state.
- The same gate covers ALL plugin commands uniformly — no per-plugin reinvention.

Trying to fight this with thread-locals (Option B) or namespace pollution (Option C) is a strict downgrade. Option A is the clean idiomatic choice, recommended by plugin-glue.md (line 490: "Recommended: A for v1").

The cost — that a misconfigured `allow_admin_from` opens destructive commands — is a CONFIGURATION HAZARD, not a plugin-level hazard. The mitigation is operator documentation and a startup-time warning (see Consequences).

## Consequences

- **Subcommand handlers don't check `is_owner`.** Gating is declarative — set `allow_admin_from` and `user_allowed_commands` in `~/.hermes/config.yaml` per the operator docs.
- **CLI mode has no slash-access policy.** When `hermes` runs as a single-user CLI, `policy_for_source` returns `enabled=False` and every command runs unguarded. CLI is implicitly single-user-owner — this is fine.
- **Operator documentation must be explicit.** The README needs a "Required configuration for production" section listing the `allow_admin_from` block, the 9 owner-only subcommands, and the consequence of misconfiguration.
- **Startup-time warning if `allow_admin_from` is unset.** Add a startup check (in `register()` body): if `cfg.is_gateway_mode and not slash_access.allow_admin_from`, log a WARNING banner: "LCM destructive subcommands are exposed to all users — set `allow_admin_from` to restrict." This is the operator-noticeable signal.
- **No per-subcommand boilerplate.** The dispatcher just routes `raw_args` to the right module without any auth ceremony.
- **OpenClaw users' muscle memory.** OpenClaw users may expect handler-side rejection messages ("operator-only — request access"). In Hermes, they get the upstream message `"⛔ /lcm is admin-only here. ..."`. Document this difference.
- **`is_owner` does NOT exist on `PluginContext`** (`docs/reference/hermes-hooks.md:120, 145`). Don't reach for it.

## Open questions / 5% uncertainty

- **Will the upstream warning be loud enough?** A WARNING-level log line on startup may be missed by less-experienced operators. Consider also surfacing the warning in `hermes plugins list` or `hermes config check`.
- **Granularity of `allow_admin_from`.** Today it's per-platform (allow_admin_from: ["@admin"] applies to Slack, Telegram, etc.). If operators want per-subcommand granularity, they'd need Option C. We bet they won't — the 9 destructive subcommands are coherent ("operator territory") and don't need finer slicing.
- **`request_context` thread-local in future Hermes versions.** If Hermes core ever adds it (Option B becomes free), revisit and add defense-in-depth checks at zero cost.
- **Test coverage.** `tests/commands/test_owner_gating.py` (per plugin-glue.md line 599) should mock the slash-access policy and confirm destructive subcommands are rejected when policy denies. This validates the END-TO-END flow even though LCM itself doesn't enforce.
