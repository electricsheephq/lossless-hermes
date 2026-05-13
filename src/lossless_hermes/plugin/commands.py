"""``/lcm`` slash command router (Epic 08-01).

Replaces the Epic 02 single-handler scaffold with the full ``/lcm`` subcommand
dispatch table. This module ships only the **router** — per-subcommand handler
bodies live in :mod:`lossless_hermes.commands` (sibling subpackage) and are
filled in by issues 08-02 through 08-15. Each handler module exposes one or
more ``run_*(parsed: ParsedLcmCommand) -> str | None`` functions consumed by
the dispatch table.

### Design — pure router + parser

The router has two pieces:

* :func:`parse_lcm_command` — pure-function token splitter over the raw
  ``ctx.args`` string. Mirrors the TS ``splitArgsQuoted`` from
  ``lossless-claw/src/plugin/lcm-command.ts:245``: honors ``--reason "..."``
  quoting, ``--from a,b,c`` comma-lists, and bare flags like ``--apply`` /
  ``--allow-main-session`` / ``--baseline`` / ``--vacuum``. The parser raises
  :class:`LcmCommandParseError` on unbalanced quotes; per-subcommand flag
  validation happens inside the handler.

* :class:`LcmCommandDispatcher` — dispatch table keyed by canonical
  subcommand path (joined by spaces). Lookups use longest-prefix match so
  ``/lcm doctor clean apply`` resolves to ``commands.doctor:run_cleaners_apply``
  while ``/lcm doctor clean`` resolves to ``commands.doctor:run_cleaners_scan``.

### Owner-gating per ADR-013

Handlers do NOT check ``owner-status`` themselves. Owner-gating is enforced
upstream by ``gateway/slash_access.SlashAccessPolicy`` BEFORE the handler
runs. The dispatcher receives only ``raw_args`` — no security context.
The "Owner-gated?" column in :data:`_SUBCOMMANDS` is documentation only;
the actual gate is in Hermes config (``allow_admin_from`` in
``~/.hermes/config.yaml``).

### Subcommand inventory

Per ``docs/porting-guides/plugin-glue.md`` "/lcm slash commands — full
inventory" the 17 logical subcommands are enumerated in :data:`_SUBCOMMANDS`.
The handler modules referenced are stubs at issue 08-01; issues 08-02..08-15
fill them in.

See:

* ``docs/adr/013-owner-gating.md`` — handler receives only ``raw_args``;
  upstream gate is the policy.
* ``docs/adr/024-project-layout.md`` — package layout decisions.
* ``docs/porting-guides/plugin-glue.md`` "/lcm slash commands" §
  — full 17-subcommand inventory.
* ``docs/reference/hermes-hooks.md`` lines 131-180 — ``register_command``
  contract.
* ``epics/08-cli-ops/08-01-slash-command-router.md`` — this issue.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, Optional

logger = logging.getLogger("lossless_hermes.plugin.commands")


# ---------------------------------------------------------------------------
# Subcommand inventory — the 17 planned /lcm subcommands
# ---------------------------------------------------------------------------
#
# Each entry: (canonical_path, handler_ref, owner_gated, description).
#
# ``canonical_path`` is the space-joined token sequence the user types after
# ``/lcm`` (e.g. ``"worker tick embedding-backfill"``). ``handler_ref`` is a
# ``"module:function"`` string resolved lazily by :meth:`_resolve_handler`
# so the router doesn't drag the entire handler tree into import time —
# subcommand stubs can do heavy imports without slowing plugin registration.
#
# ``owner_gated`` is DOCUMENTATION ONLY. Per ADR-013, the dispatcher does
# NOT check it — Hermes's ``SlashAccessPolicy`` runs upstream of dispatch.
# The flag drives the ``(admin)`` marker in ``/lcm help`` output so
# operators can see at a glance which subcommands their ``allow_admin_from``
# config gates.
_SUBCOMMANDS: list[tuple[str, str, bool, str]] = [
    # The 17 logical subcommands documented in plugin-glue.md "/lcm slash
    # commands — full inventory" + issue 08-01 spec line 25. The router
    # dispatches by longest-prefix match on the canonical_path field;
    # entries that share a handler ref (e.g. "worker" / "worker status",
    # or the two reconcile variants) route to the same module:function.
    #
    # Status / help (always-on)
    (
        "status",
        "lossless_hermes.commands.status:run",
        False,
        "Engine health snapshot — db, conversations, token state",
    ),
    ("help", "lossless_hermes.commands.help:run", False, "List available /lcm subcommands"),
    # Health / diagnostics
    (
        "health",
        "lossless_hermes.commands.health:run",
        False,
        "v4.1 health snapshot — workers + embeddings backlog",
    ),
    (
        "doctor",
        "lossless_hermes.commands.doctor:run_scan",
        False,
        "Read-only scan for orphaned / inconsistent rows",
    ),
    (
        "doctor apply",
        "lossless_hermes.commands.doctor:run_apply",
        True,
        "Re-summarize problematic rows surfaced by doctor",
    ),
    (
        "doctor clean",
        "lossless_hermes.commands.doctor:run_cleaners_scan",
        True,
        "List high-confidence junk rows by cleaner",
    ),
    (
        "doctor clean apply",
        "lossless_hermes.commands.doctor:run_cleaners_apply",
        True,
        "Apply doctor cleaners (DELETEs rows; optional vacuum)",
    ),
    # Worker control
    (
        "worker",
        "lossless_hermes.commands.worker:run_status",
        False,
        "Inspect background worker queue + last-tick time",
    ),
    (
        "worker status",
        "lossless_hermes.commands.worker:run_status",
        False,
        "Inspect background worker queue + last-tick time (alias)",
    ),
    (
        "worker tick embedding-backfill",
        "lossless_hermes.commands.worker:run_tick_backfill",
        True,
        "Force a worker tick (embedding-backfill); burns paid quota",
    ),
    # Maintenance / data ops
    (
        "backup",
        "lossless_hermes.commands.backup:run",
        False,
        "VACUUM INTO a timestamped .bak file in HERMES_HOME",
    ),
    (
        "rotate",
        "lossless_hermes.commands.rotate:run",
        False,
        "Rotate session storage; JSONL-dependent, may drop",
    ),
    # Session keys — list & apply route through the same handler module;
    # the handler branches on parsed.flags["list_candidates"] vs
    # parsed.flags["apply"]. The two help-table entries document the
    # operator-visible shapes.
    (
        "reconcile-session-keys --list-candidates",
        "lossless_hermes.commands.reconcile:run_list",
        True,
        "List session_keys that look like duplicates / drift candidates",
    ),
    (
        "reconcile-session-keys --apply",
        "lossless_hermes.commands.reconcile:run_apply",
        True,
        "Rewrite session_key on conversations + summaries",
    ),
    # Purge
    (
        "purge",
        "lossless_hermes.commands.purge:run",
        True,
        "Soft-suppress leaves + cascade; requires --reason argument",
    ),
    # Eval (Epic 09)
    (
        "eval",
        "lossless_hermes.commands.eval:run",
        True,
        "Eval harness against fts/semantic/hybrid backends",
    ),
    # Import (Epic 08, separate cli module)
    (
        "import-openclaw",
        "lossless_hermes.cli.import_openclaw:run_slash",
        True,
        "Import OpenClaw LCM snapshot",
    ),
]


# Back-compat re-export for tests that still import the Epic-02 inventory
# name. The shape here is intentionally similar (3-tuple) so callers can
# iterate without churn. Removed in a follow-up once 08-02..08-15 land.
_SUBCOMMAND_INVENTORY: list[tuple[str, str, str]] = [
    (name, "Epic 08" if "Epic 09" not in desc and name != "eval" else "Epic 09", desc)
    for (name, _handler, _gated, desc) in _SUBCOMMANDS
]


# ---------------------------------------------------------------------------
# Parser — splits ctx.args into a ParsedLcmCommand
# ---------------------------------------------------------------------------


class LcmCommandParseError(ValueError):
    """Raised by :func:`parse_lcm_command` on argument-parse errors.

    Wraps the offending token (or shlex error message) in ``args[0]`` so
    the dispatcher can echo it back to the user without exposing a stack
    trace. Per the issue 08-01 acceptance criterion: "raises
    LcmCommandParseError with the offending token".
    """


@dataclass
class ParsedLcmCommand:
    """Result of parsing a raw ``/lcm <args>`` string.

    Attributes:
        name: Canonical subcommand path matched in :data:`_SUBCOMMANDS`
            (e.g. ``"doctor clean apply"``). Empty string for bare
            ``/lcm`` (aliased to ``status``). ``"<unknown>"`` if no
            subcommand matched — handler is the "unknown subcommand"
            fallback (returns help text).
        raw_args: The original raw_args string (everything after
            ``/lcm`` — same value Hermes passed to ``handle``). Kept so
            handlers needing custom flag parsing (purge, reconcile,
            eval) can re-tokenize via :func:`shlex.split` themselves.
        tokens: All tokens after the canonical subcommand path is
            consumed. For ``/lcm purge --reason "x" --session-key k``
            with name ``"purge"``, ``tokens == ["--reason", "x",
            "--session-key", "k"]``.
        flags: Lightweight pre-parse of common flags — ``--from``
            (comma-list → ``list[str]``), ``--reason`` (string),
            ``--apply`` / ``--baseline`` / ``--allow-main-session`` /
            ``--vacuum`` (bare flags → ``True``). Handlers may also do
            their own per-subcommand flag parsing on ``tokens`` for
            cases not covered here. The pre-parse is best-effort —
            unknown flags survive in ``tokens`` for the handler.
    """

    name: str
    raw_args: str = ""
    tokens: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)


def parse_lcm_command(raw_args: str | None) -> ParsedLcmCommand:
    """Parse ``raw_args`` into a :class:`ParsedLcmCommand`.

    Token splitter that honors:

    * ``--reason "all rebase work"`` (quoted multi-word values via
      :func:`shlex.split`).
    * ``--from k1,k2,k3`` (comma-separated lists → ``flags["from"]`` as
      ``list[str]``).
    * Bare flags ``--apply`` / ``--baseline`` / ``--allow-main-session``
      / ``--vacuum`` → ``flags["<name>"] = True``.

    On unbalanced quotes, raises :class:`LcmCommandParseError` with the
    underlying shlex message — the dispatcher's :meth:`LcmCommandDispatcher.handle`
    catches this and returns a friendly text to the user.

    Per ADR-013 this is a pure function over the input string — no DB
    access, no security check. The result drives dispatch.

    Args:
        raw_args: Everything after ``/lcm `` (or empty for bare
            ``/lcm``). Passed through ``str | None`` for robustness
            against Hermes ever sending ``None``.

    Returns:
        A :class:`ParsedLcmCommand` with ``name`` set to the longest
        matching canonical path in :data:`_SUBCOMMANDS`, or empty string
        (bare ``/lcm``) which the dispatcher aliases to ``status``, or
        ``"<unknown>"`` when no subcommand matched.

    Raises:
        LcmCommandParseError: ``shlex.split`` raised on unbalanced
            quotes. The error message preserves the underlying detail.
    """
    text = (raw_args or "").strip()
    if not text:
        return ParsedLcmCommand(name="", raw_args=text)

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise LcmCommandParseError(f"argument parse error — {exc!s}") from exc

    if not tokens:
        return ParsedLcmCommand(name="", raw_args=text)

    # Longest-prefix match against _SUBCOMMANDS canonical paths. We sort
    # by token-length descending so "doctor clean apply" wins over
    # "doctor clean" wins over "doctor", and
    # "reconcile-session-keys --apply" wins over a bare
    # "reconcile-session-keys" first-token guess.
    name = "<unknown>"
    consumed = 0
    candidates = sorted(
        (entry[0] for entry in _SUBCOMMANDS),
        key=lambda p: len(p.split()),
        reverse=True,
    )
    lowered_tokens = [t.lower() for t in tokens]
    for path in candidates:
        path_tokens = path.split()
        if len(path_tokens) > len(tokens):
            continue
        if lowered_tokens[: len(path_tokens)] == path_tokens:
            name = path
            consumed = len(path_tokens)
            break

    # Fallback: if no full match but the first token is the head of one
    # or more multi-token canonical paths (e.g. user typed bare
    # "reconcile-session-keys" without --list-candidates / --apply), pick
    # an entry that documents the expected shape. The handler is
    # responsible for returning a "missing required flag" error message.
    # This keeps the unknown-subcommand branch reserved for true typos.
    if name == "<unknown>" and len(tokens) >= 1:
        first = lowered_tokens[0]
        for path in candidates:
            head = path.split()[0]
            if head == first:
                name = path
                consumed = 1  # only the leading token; flags survive in tokens
                break

    remaining = tokens[consumed:]

    # Best-effort flag pre-parse over the FULL token list (not just the
    # residual), so flags that the canonical-path match also consumed
    # (e.g. ``--list-candidates``, ``--apply`` in ``reconcile-session-keys
    # --apply``) still appear in ``parsed.flags`` for handler ergonomics.
    # Per-subcommand parsers (in handler modules) re-process ``tokens``
    # for cases not covered here, but pulling ``--reason "..."`` and
    # ``--from a,b,c`` out at the router level catches the load-bearing
    # patterns from the TS source (see plugin-glue.md "/lcm slash commands"
    # row "reconcile-session-keys", "purge").
    flags = _preparse_flags(tokens)

    return ParsedLcmCommand(
        name=name,
        raw_args=text,
        tokens=remaining,
        flags=flags,
    )


def _preparse_flags(tokens: list[str]) -> dict[str, Any]:
    """Pre-parse common ``--xxx`` flags out of a token list.

    Bare flags become ``flags[name] = True``; valued flags consume the
    following token. Unknown ``--xxx`` tokens are ignored — they survive
    in ``ParsedLcmCommand.tokens`` for the handler to parse.

    Raises :class:`LcmCommandParseError` if a valued flag has no
    following token (``--reason`` at end of input, etc.).
    """
    flags: dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in (
            "--apply",
            "--baseline",
            "--allow-main-session",
            "--vacuum",
            "--list-candidates",
        ):
            # Bare flags. Stored without the leading "--" and with dashes
            # → underscores for ergonomic handler access
            # (``parsed.flags.get("allow_main_session")``).
            flags[token[2:].replace("-", "_")] = True
        elif token == "--reason":
            if i + 1 >= len(tokens):
                raise LcmCommandParseError("`--reason` requires a quoted value")
            flags["reason"] = tokens[i + 1]
            i += 1
        elif token == "--from":
            if i + 1 >= len(tokens):
                raise LcmCommandParseError("`--from` requires a comma-separated list")
            flags["from"] = [p.strip() for p in tokens[i + 1].split(",") if p.strip()]
            i += 1
        elif token == "--to":
            if i + 1 >= len(tokens):
                raise LcmCommandParseError("`--to` requires a session_key value")
            flags["to"] = tokens[i + 1]
            i += 1
        # All other tokens (positional args, per-subcommand flags) stay
        # in ``tokens`` for the handler to parse.
        i += 1
    return flags


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class LcmCommandDispatcher:
    """Router for ``/lcm <subcommand> [args]``.

    The dispatcher holds a reference to the :class:`LCMEngine` so handler
    modules can read engine state without re-importing module globals.
    Construction is light — no DB open, no network call. Per ADR-013 no
    security state is held; gating happens upstream of :meth:`handle`.

    Args:
        engine: The :class:`LCMEngine` instance. Passed to every handler
            via ``parsed.engine`` (handlers can read engine state but
            never write).

    Example::

        dispatcher = LcmCommandDispatcher(engine)
        ctx.register_command("lcm", dispatcher.handle, args_hint="<subcommand>")
    """

    def __init__(self, engine: Any) -> None:
        self.engine = engine

    def handle(self, raw_args: str) -> str:
        """Route ``/lcm <subcommand> [args]`` to the right handler.

        Per ``docs/reference/hermes-hooks.md`` line 131-180 the signature
        is ``(raw_args: str) -> str | None``. ADR-013 forbids security
        checks here — Hermes's upstream gate runs first.

        Empty / whitespace-only args alias to ``status`` for TS parity
        (operators expect ``/lcm`` to be a status query — see doctor-ops.md
        §"Operator gate"). Unknown subcommands return the help text with
        an "Unknown subcommand" prefix.

        Args:
            raw_args: Everything after ``/lcm ``. Empty string when the
                user typed bare ``/lcm``.

        Returns:
            A string for Hermes to render to the user. On any handler
            exception, returns ``"/lcm <sub> failed: <exc>"`` to keep the
            dispatcher robust (don't crash the chat session on a bug).
        """
        try:
            parsed = parse_lcm_command(raw_args)
        except LcmCommandParseError as exc:
            return f"/lcm: {exc!s}. Run /lcm help."

        # Bare /lcm aliases to status (TS parity).
        name = parsed.name or "status"
        if name == "<unknown>":
            first_token = (parsed.raw_args.split(maxsplit=1) or [""])[0]
            return (
                f"/lcm: unknown subcommand `{first_token}`. "
                "Run /lcm help for the full subcommand inventory."
            )

        # Attach engine to the parsed object so the handler can read it
        # without re-importing the dispatcher. Stored as a plain
        # attribute (not in flags) since it's a non-arg dependency.
        setattr(parsed, "engine", self.engine)

        handler = self._resolve_handler(name)
        if handler is None:
            target_epic = self._target_epic_for(name)
            return (
                f"/lcm {name}: subcommand not yet implemented ({target_epic}). "
                "Run /lcm help for the full subcommand inventory."
            )

        try:
            result = handler(parsed)
        except Exception as exc:  # noqa: BLE001 — robust dispatcher
            logger.exception("[lcm] handler error in subcommand %r", name)
            return f"/lcm {name} failed: {exc!s}"
        return result if result is not None else ""

    # ---------------------------------------------------------------------
    # Handler resolution — lazy import per subcommand
    # ---------------------------------------------------------------------

    def _resolve_handler(self, name: str) -> Optional[Callable[[ParsedLcmCommand], Optional[str]]]:
        """Resolve a canonical subcommand path to its handler callable.

        Lazy-imports the handler module to keep ``register()`` light —
        importing every subcommand body at register time would pull in
        sqlite, Voyage, eval, doctor, and reconcile machinery before the
        first slash command runs. Per ADR-024 §"Project layout" the
        ``plugin/`` package stays thin; ``commands/`` holds the bodies.

        Returns ``None`` when the handler module / function doesn't
        exist yet (Epic 08-NN hasn't shipped). The dispatcher renders the
        "not yet implemented" message for those.

        Args:
            name: Canonical subcommand path (one of the
                :data:`_SUBCOMMANDS` ``canonical_path`` entries).

        Returns:
            The resolved callable, or ``None`` if not implemented.
        """
        for path, handler_ref, _gated, _desc in _SUBCOMMANDS:
            if path == name:
                module_name, func_name = handler_ref.split(":", 1)
                try:
                    module = import_module(module_name)
                except ImportError:
                    return None
                func = getattr(module, func_name, None)
                if not callable(func):
                    return None
                return func
        return None

    def _target_epic_for(self, name: str) -> str:
        """Return the epic tag for ``name`` (used by the not-implemented msg)."""
        # eval is the only Epic-09 subcommand. Everything else is Epic 08
        # (the catch-all bucket per the issue spec).
        if name == "eval":
            return "Epic 09"
        return "Epic 08"


__all__ = [
    "LcmCommandDispatcher",
    "LcmCommandParseError",
    "ParsedLcmCommand",
    "_SUBCOMMANDS",
    "_SUBCOMMAND_INVENTORY",  # back-compat for Epic-02 tests
    "parse_lcm_command",
]
