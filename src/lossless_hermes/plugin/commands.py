"""``/lcm`` slash command dispatcher — Epic 02-10 seam.

This module ports the dispatcher skeleton from
``lossless-claw/src/plugin/lcm-command.ts`` (2884 LOC TS). At issue 02-10 only
the dispatcher + ``status`` + ``help`` bodies are wired; every other
subcommand returns the Epic-08 "not yet implemented" stub so the surface is
discoverable but the bodies don't block Epic 02.

### Design — class-based dispatcher

The dispatcher is a class (:class:`LcmCommandDispatcher`) rather than module
globals so the engine reference is bound at construction time (per
:func:`lossless_hermes.register`) without needing module-level state. The
class exposes a single bound method :meth:`handle` whose signature is
``(raw_args: str) -> str`` — exactly what Hermes's ``register_command``
expects (see ``hermes_cli/plugins.py:401-453``).

### Owner-gating

Per ADR-013 §Decision, handlers do NOT check ``is_owner`` themselves.
Owner-gating is enforced upstream by
``gateway/slash_access.SlashAccessPolicy`` before the handler runs. This
dispatcher receives only ``raw_args`` — no security context, no user
identity. The "destructive" annotations in :data:`_SUBCOMMAND_INVENTORY`
are documentation only; the actual gate is in Hermes config (``allow_admin_from``
in ``~/.hermes/config.yaml``).

### Subcommand inventory (19 entries)

Per ``docs/porting-guides/plugin-glue.md`` "/lcm slash commands" section.
The two Epic-02 implementations (``status`` and ``help``) are the only
real handlers at 02-10; the other 17 are stubs returning the standard
"not yet implemented" message. Epic 08 fills in the bodies.

### Subcommand-arg parsing

The dispatcher uses :func:`shlex.split` so quoted args survive ("``--reason
"a b"``" parses as a single token). The first token is the subcommand;
remaining tokens are joined back into ``sub_args`` for the handler. Nested
subcommands (``doctor apply``, ``worker tick``, ``prompts add``) are
single-level routes at 02-10 — they go to the parent stub (``doctor``,
``worker``, ``prompts``). Epic 08's bodies parse the inner subcommand
themselves.

See:

* ``docs/adr/013-owner-gating.md`` — handler receives only ``raw_args``.
* ``docs/porting-guides/plugin-glue.md`` "/lcm slash commands" §
  — the full 19-subcommand inventory and target epic for each.
* ``docs/reference/hermes-hooks.md`` lines 131-180 — ``register_command``
  contract.
* ``epics/02-engine-skeleton/02-10-slash-command-dispatcher.md`` — this
  issue's acceptance criteria.
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

logger = logging.getLogger("lossless_hermes.plugin.commands")


# ---------------------------------------------------------------------------
# Subcommand inventory — the 19 planned /lcm subcommands
# ---------------------------------------------------------------------------
#
# Each entry: (name, target_epic, description).
#
# ``target_epic`` is the documentation target where the body lands; at 02-10
# only ``status`` and ``help`` are real. The inventory is the source of truth
# for the ``/lcm help`` output and the "known but not yet implemented"
# routing logic.
#
# Per ``docs/porting-guides/plugin-glue.md`` "Owner-gating count: 9 out of
# 13"; the destructive subcommands (purge, doctor apply, doctor clean,
# reconcile-session-keys, worker tick, eval, eval-run, rotate, db-backup)
# are operator-only by config. The dispatcher itself does NOT gate (ADR-013).
_SUBCOMMAND_INVENTORY: list[tuple[str, str, str]] = [
    # --- Implemented in Epic 02 ---
    ("status", "Epic 02", "Engine health snapshot — db, conversations, token state"),
    ("help", "Epic 02", "List available /lcm subcommands and their target epic"),
    # --- Health / diagnostics — Epic 08 ---
    ("health", "Epic 08", "v4.1 health snapshot — workers + embeddings backlog"),
    ("doctor", "Epic 08", "Read-only scan for orphaned / inconsistent rows"),
    ("doctor apply", "Epic 08", "(owner) Re-summarize problematic rows surfaced by doctor"),
    (
        "doctor cleaners",
        "Epic 08",
        "(owner) List/cleanup high-confidence junk rows by cleaner",
    ),
    # --- Worker control — Epic 08 ---
    ("worker status", "Epic 08", "Inspect background worker queue + last-tick time"),
    (
        "worker tick",
        "Epic 08",
        "(owner) Force a worker tick (e.g. embedding-backfill); burns paid quota",
    ),
    # --- Maintenance / data ops — Epic 08 ---
    ("backup", "Epic 08", "VACUUM INTO a timestamped .bak file in HERMES_HOME"),
    ("rotate", "Epic 08", "Rotate session storage; JSONL-dependent, may drop"),
    ("db-backup", "Epic 08", "(owner) Snapshot LCM SQLite DB to a backup file"),
    ("db-info", "Epic 08", "DB file path, size, schema version, last-migration timestamp"),
    # --- Session keys — Epic 08 ---
    (
        "reconcile-session-keys",
        "Epic 08",
        "(owner) List/apply session_key rewrites across conversations + summaries",
    ),
    # --- Purge — Epic 08 ---
    (
        "purge",
        "Epic 08",
        "(owner) Soft-suppress leaves + cascade; requires --reason argument",
    ),
    # --- Prompts — Epic 08 ---
    ("prompts", "Epic 08", "Prompt library overview"),
    ("prompts list", "Epic 08", "List configured prompt templates"),
    ("prompts add", "Epic 08", "(owner) Add a new prompt template"),
    # --- Eval — Epic 09 ---
    ("eval", "Epic 09", "(owner) Eval harness against fts/semantic/hybrid backends"),
    ("eval-run", "Epic 09", "(owner) Trigger a named eval run; paid embedding cost"),
]


class LcmCommandDispatcher:
    """Dispatcher for the ``/lcm <subcommand> [args]`` slash command.

    The dispatcher holds a reference to the :class:`LCMEngine` so handlers
    can read engine state (token counters, store counts, etc.) without
    re-importing module globals. Construction is light — no DB open, no
    network call. Per ADR-013, no security state is held; gating happens
    upstream of :meth:`handle`.

    At issue 02-10 the implemented subcommands are ``status`` and ``help``.
    All others return the "not yet implemented" stub for their target epic.

    Args:
        engine: The :class:`LCMEngine` instance. Used by ``status`` to read
            the inherited ``ContextEngine.get_status()`` dict plus LCM-specific
            fields. Tests may pass a mock or a real engine; the dispatcher
            only reads from it (never writes).

    Example::

        dispatcher = LcmCommandDispatcher(engine)
        ctx.register_command("lcm", dispatcher.handle, args_hint="<subcommand>")
    """

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    # ---------------------------------------------------------------------
    # Entry point — invoked by Hermes per the register_command contract
    # ---------------------------------------------------------------------

    def handle(self, raw_args: str) -> str:
        """Route ``/lcm <subcommand> [args]`` to the right handler.

        Per ``docs/reference/hermes-hooks.md`` line 131-180 the signature is
        ``(raw_args: str) -> str | None``. ADR-013 requires no security
        check here — Hermes's upstream gate runs first.

        The parser is :func:`shlex.split` so quoted args survive
        (``--reason "a b"`` parses as one token). The first token is the
        subcommand; remaining tokens are joined and passed to the handler
        for its own parsing.

        Per the Epic 02-10 scope, only ``status`` and ``help`` route to
        real bodies. Every other subcommand — whether listed in
        :data:`_SUBCOMMAND_INVENTORY` (purge, doctor, worker, …) or
        completely unknown (bogus typos) — returns the standard
        "not yet implemented (Epic 08)" message. Epic 08 fills in the
        per-subcommand bodies and at that point the dispatcher will
        distinguish "known stub" from "unknown" routing.

        The :data:`_SUBCOMMAND_INVENTORY` is used by ``/lcm help`` to
        render the markdown table but not by routing at 02-10 — the
        unified stub keeps the seam simple.

        Args:
            raw_args: Everything after ``/lcm ``. Empty string when the
                user typed bare ``/lcm`` — aliased to ``status``.

        Returns:
            A string for Hermes to render to the user. On any handler
            exception, returns ``"/lcm <sub> failed: <exc>"`` to keep the
            dispatcher robust (don't crash the chat session on a bug).
        """
        # Defensive parse. Empty / None / whitespace-only → bare /lcm,
        # which aliases to `status` per the Epic 02 spec.
        try:
            tokens = shlex.split(raw_args or "")
        except ValueError as exc:
            # shlex raises ValueError on unbalanced quotes.
            return f"/lcm: argument parse error — {exc!s}. Run /lcm help."

        if not tokens:
            return self._handle_status("")

        subcommand = tokens[0]
        sub_args = " ".join(shlex.quote(t) for t in tokens[1:])

        # Exact match first — only ``status`` and ``help`` route to real
        # bodies at 02-10. Everything else falls through to the stub.
        handler = self._exact_handlers().get(subcommand)
        if handler is not None:
            try:
                return handler(sub_args)
            except Exception as exc:  # noqa: BLE001 — robust dispatcher
                logger.exception(
                    "[lcm] handler error in subcommand %r",
                    subcommand,
                )
                return f"/lcm {subcommand} failed: {exc!s}"

        # Every other subcommand returns the standard "not yet
        # implemented" message. Per the Epic 02-10 scope:
        # > Every other subcommand → returns
        # > "subcommand <X> not yet implemented (Epic 08)"
        #
        # Epic 08 replaces this branch with per-subcommand routing.
        target_epic = self._target_epic_for(subcommand)
        return self._not_yet_implemented(subcommand, target_epic)

    # ---------------------------------------------------------------------
    # Implemented subcommand bodies — Epic 02
    # ---------------------------------------------------------------------

    def _handle_status(self, _sub_args: str) -> str:
        """``/lcm status`` — engine health snapshot.

        Reads from :meth:`ContextEngine.get_status` (inherited on
        :class:`LCMEngine`) plus a few LCM-specific fields. At 02-10 the
        DB may not be open (heavy init defers to ``on_session_start`` per
        ADR-001) — we degrade gracefully when stores are ``None``.

        Maps to ``engine.ts:plugin/lcm-command.ts`` case ``"status"`` —
        Epic 02 ships a small status block; Epic 08 grows it to the full
        OpenClaw output per ``docs/porting-guides/plugin-glue.md`` line
        426.
        """
        engine = self.engine

        # Pull the standard ABC-defined status dict. Any context engine
        # exposes this — see hermes-agent/agent/context_engine.py:173.
        try:
            status = engine.get_status() or {}
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning("[lcm] engine.get_status() raised: %s", exc)
            status = {}

        # LCM-specific augmentation. ``None`` when on_session_start
        # hasn't run yet (pre-Epic-02-03 timeline, or in tests).
        db_open = getattr(engine, "_db", None) is not None
        conv_store = getattr(engine, "_conversation_store", None)

        lines = [
            "[lcm] status",
            f"  engine: {getattr(engine, 'name', '<unknown>')}",
            f"  db: {'open' if db_open else 'not opened (on_session_start pending)'}",
        ]

        if conv_store is not None:
            try:
                # ``get_message_count`` is the available Epic-01 surface;
                # there is no ``count_conversations`` method on the store
                # yet (Epic 08 may add one). We log a generic "store
                # available" until then.
                lines.append("  conversation_store: ready")
            except Exception:  # noqa: BLE001
                lines.append("  conversation_store: error")
        else:
            lines.append("  conversation_store: not initialized")

        # Standard ABC fields.
        lines.append(f"  last_prompt_tokens: {status.get('last_prompt_tokens', 0)}")
        lines.append(f"  threshold_tokens: {status.get('threshold_tokens', 0)}")
        lines.append(f"  context_length: {status.get('context_length', 0)}")
        lines.append(f"  usage_percent: {status.get('usage_percent', 0):.1f}")
        lines.append(f"  compression_count: {status.get('compression_count', 0)}")
        lines.append("  ok")

        return "\n".join(lines)

    def _handle_help(self, _sub_args: str) -> str:
        """``/lcm help`` — markdown table of the 19 planned subcommands.

        The table groups the inventory by target epic so operators can
        see at a glance what's available now (Epic 02) versus what
        lands later. Per Epic 02 README this is a documentation-only
        surface — the rendering is operator-readable markdown.

        Maps to ``engine.ts:plugin/lcm-command.ts`` case ``"help"``.
        """
        lines = [
            "# /lcm subcommands",
            "",
            "| Subcommand | Target | Description |",
            "| --- | --- | --- |",
        ]
        for name, target_epic, desc in _SUBCOMMAND_INVENTORY:
            lines.append(f"| `/lcm {name}` | {target_epic} | {desc} |")
        lines.append("")
        lines.append(
            "Owner-gating: destructive subcommands (purge, doctor apply, "
            "doctor cleaners, reconcile-session-keys, worker tick, eval, "
            "eval-run, db-backup, prompts add) are gated upstream by "
            "Hermes's `allow_admin_from` config. See ADR-013."
        )
        return "\n".join(lines)

    # ---------------------------------------------------------------------
    # Stub helpers
    # ---------------------------------------------------------------------

    def _not_yet_implemented(self, subcommand: str, target_epic: str) -> str:
        """Standard "not yet implemented" message for known stubs.

        Format: ``"subcommand <X> not yet implemented (Epic 08)"`` — matches
        the user-facing spec. Operators running ``/lcm help`` see the same
        target epic in the table; this message is the per-command echo.
        """
        return (
            f"/lcm {subcommand}: subcommand not yet implemented ({target_epic}). "
            "Run /lcm help for the full subcommand inventory."
        )

    def _target_epic_for(self, first_token: str) -> str:
        """Look up the target epic for a subcommand by its first token.

        Used by the "not yet implemented" path:

        * For known parents (``doctor``, ``worker``, ``prompts``) returns
          the inventory's target epic ("Epic 08" or "Epic 09").
        * For ``doctor apply`` / ``worker tick`` / etc. nested forms we
          look at the first token only — the first-token match in the
          inventory wins (``doctor`` → Epic 08).
        * For truly unknown subcommands not in the inventory, the
          fallback is ``"Epic 08"`` since that's the catch-all bucket
          where the user-facing spec puts every missing handler.
        """
        # Exact match on full subcommand name (handles single-token
        # entries like "purge" and "health").
        for name, target_epic, _ in _SUBCOMMAND_INVENTORY:
            if name == first_token:
                return target_epic
        # First-token match for nested entries — e.g. "doctor apply"
        # carries the same Epic-08 tag as "doctor".
        for name, target_epic, _ in _SUBCOMMAND_INVENTORY:
            if name.startswith(first_token + " "):
                return target_epic
        # Fallback for truly unknown subcommands. Per the Epic 02-10
        # spec, the catch-all is "Epic 08".
        return "Epic 08"

    # ---------------------------------------------------------------------
    # Dispatch table — exact subcommand name → method
    # ---------------------------------------------------------------------

    def _exact_handlers(self) -> dict[str, Any]:
        """Return the exact-match handler table.

        Built lazily so subclasses (Epic 08) can extend by overriding this
        method and merging additional entries. At 02-10 only ``status``
        and ``help`` route to real bodies; everything else returns a stub.

        Entries are bound methods on ``self``; the dispatcher calls them
        as ``handler(sub_args)``.
        """
        return {
            "status": self._handle_status,
            "help": self._handle_help,
        }
