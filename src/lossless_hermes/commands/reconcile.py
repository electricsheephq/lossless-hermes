"""``/lcm reconcile-session-keys`` — session-key merge handler (Epic 08-05).

Replaces the Epic 08-01 stub with a real handler that:

* ``run_list`` — parses ``/lcm reconcile-session-keys --list-candidates``,
  calls :func:`lossless_hermes.operator.reconcile.list_legacy_candidates`,
  and formats the result as the operator-facing text block.
* ``run_apply`` — parses ``/lcm reconcile-session-keys --apply --from
  k1,k2 --to k3 --reason "..." [--allow-main-session]``, delegates to
  :func:`lossless_hermes.operator.reconcile.reconcile_session_keys`, and
  formats the result.

Ports the TS ``case "reconcile-session-keys"`` parser at
``lossless-claw/src/plugin/lcm-command.ts:473-515`` plus the
``buildReconcileListText`` renderer at lines 1930-1962 and the
``buildReconcileText`` renderer at lines 2128-2183.

### CLI surface (per ``plugin-glue.md`` "/lcm slash commands" lines 435-436)

::

    /lcm reconcile-session-keys --list-candidates
    /lcm reconcile-session-keys --apply --from k1,k2 --to k3 --reason "..."
      [--allow-main-session]

* ``--list-candidates`` and ``--apply`` are mutually exclusive.
* ``--apply`` requires all of ``--from``, ``--to``, ``--reason``.
* ``--allow-main-session`` is required to merge INTO
  ``agent:main:main`` (safeguard against accidentally clobbering the
  operator's primary thread).

### Output shape

Mirrors the TS ``buildReconcileListText`` / ``buildReconcileText``
output as plain text (the Hermes ``register_command`` contract returns
``str``). The handler renders three section types:

* **List mode** — "Legacy candidates" + per-candidate line + "Next step"
  hint that shows the ``--apply`` template.
* **Apply mode — happy path** — "Plan" (echo back parsed args) +
  "Apply" (counts + summary line).
* **Apply mode — error** — "Plan" + "Apply" with ``status=failed`` +
  the :class:`ReconcileError` kind / message.

### DB access

The handler reads ``parsed.engine`` (set by
:class:`lossless_hermes.plugin.commands.LcmCommandDispatcher.handle`)
and obtains the SQLite connection from the engine. When the engine has
no DB connection (misconfiguration or pre-init), returns a friendly
"DB unavailable" text and does NOT raise.

### Owner-gating per ADR-013

Owner-gating is **upstream** — Hermes's ``SlashAccessPolicy`` gates the
``allow_admin_from`` config before this handler runs. The handler does
NOT check ``is_owner`` itself. Wave-12 P1 fix: BOTH ``--list-candidates``
AND ``--apply`` are owner-gated (the list path exposes ``session_key``
+ first-message previews across the entire conversation set per
``docs/porting-guides/doctor-ops.md`` §"Operator gate" line 336).

See:

* ``epics/08-cli-ops/08-05-reconcile-session-keys.md`` — this issue.
* ``src/lossless_hermes/operator/reconcile.py`` — the operator body.
* ``docs/adr/013-owner-gating.md`` — caller-side gating, not
  handler-side.
* ``lossless-claw/src/plugin/lcm-command.ts:347-515, 1930-1962,
  2128-2183`` — TS source pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from lossless_hermes.operator.reconcile import (
    ReconcileArgs,
    ReconcileError,
    list_legacy_candidates,
    reconcile_session_keys,
)

logger = logging.getLogger("lossless_hermes.commands.reconcile")


# ---------------------------------------------------------------------------
# Parsed-args dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ApplyArgs:
    """Internal result of :func:`_parse_apply_args`.

    Pre-validation per-flag values. The handler combines these into a
    :class:`ReconcileArgs` after calling validation; the operator-facing
    error path is "echo back what the parser saw + the reason it
    rejected" rather than crashing with a stack trace.
    """

    from_session_keys: list[str]
    to_session_key: str | None = None
    reason: str = ""
    allow_main_session: bool = False
    parse_error: str | None = None


# ---------------------------------------------------------------------------
# Public handlers — invoked by LcmCommandDispatcher
# ---------------------------------------------------------------------------


def run_list(parsed: Any) -> str:
    """``/lcm reconcile-session-keys --list-candidates`` handler.

    Owner-gated (Wave-12 P1 per doctor-ops.md §"Operator gate" line 336 —
    listing exposes ``session_key`` + first-message previews across the
    entire conversation set, so gating applies even though list is
    read-only).

    Args:
        parsed: :class:`ParsedLcmCommand`. ``parsed.tokens`` is the flag
            list after the canonical-path was consumed;
            ``parsed.engine`` is the :class:`LCMEngine` instance.

    Returns:
        Multi-line operator-facing text block. Always non-empty; never
        raises (per the dispatcher's "be robust" contract).
    """
    # Per TS lcm-command.ts:487-488 — combining --list-candidates with
    # --apply is rejected up-front.
    if parsed.flags.get("apply"):
        return _build_text(
            sections=[
                (
                    "Apply",
                    [
                        ("status", "rejected"),
                        ("kind", "list_and_apply"),
                        (
                            "fix",
                            "`/lcm reconcile-session-keys` cannot combine "
                            "`--list-candidates` with `--apply`. Use one or "
                            "the other.",
                        ),
                    ],
                ),
            ],
        )

    db = _resolve_db(parsed)
    if db is None:
        return _build_text(
            sections=[
                (
                    "Legacy candidates",
                    [
                        ("status", "unavailable"),
                        (
                            "reason",
                            "engine DB connection not available (engine pre-init?)",
                        ),
                    ],
                ),
            ],
        )

    try:
        candidates = list_legacy_candidates(db)
    except sqlite3.Error as exc:
        logger.exception("[reconcile] list_legacy_candidates failed")
        return _build_text(
            sections=[
                (
                    "Legacy candidates",
                    [("status", "failed"), ("reason", str(exc))],
                ),
            ],
        )

    if len(candidates) == 0:
        return _build_text(
            sections=[
                (
                    "Legacy candidates",
                    [("matched session keys", "0")],
                ),
                ("Result", ["No `legacy:conv_*` session keys present."]),
            ],
        )

    candidate_lines = [
        f"`{c.session_key}` · convs={c.conversation_count} · leaves={c.leaf_count}"
        for c in candidates
    ]
    return _build_text(
        sections=[
            (
                "Legacy candidates",
                [("matched session keys", str(len(candidates)))],
            ),
            ("Candidates", candidate_lines),
            (
                "Next step",
                [
                    "`/lcm reconcile-session-keys --apply --from k1,k2 "
                    '--to my-thread --reason "..."` merges the listed keys.'
                ],
            ),
        ],
    )


def run_apply(parsed: Any) -> str:
    """``/lcm reconcile-session-keys --apply`` handler.

    Owner-gated. Rewrites ``conversations.session_key`` + ``summaries.session_key``
    from each ``--from`` key to the ``--to`` key, and writes one audit row
    per affected conversation to ``lcm_session_key_audit``. All steps run
    in one ``BEGIN IMMEDIATE`` transaction.

    Args:
        parsed: :class:`ParsedLcmCommand`. ``parsed.tokens`` is the flag
            list; ``parsed.flags`` contains the router-level pre-parse
            of ``--from`` / ``--to`` / ``--reason`` /
            ``--allow-main-session``; ``parsed.engine`` is the
            :class:`LCMEngine` instance.

    Returns:
        Multi-line operator-facing text block. Always non-empty; never
        raises (per the dispatcher's "be robust" contract).
    """
    args = _parse_apply_args(parsed)

    plan_section = (
        "Plan",
        [
            (
                "from",
                ", ".join(f"`{k}`" for k in args.from_session_keys)
                if args.from_session_keys
                else "(none)",
            ),
            (
                "to",
                f"`{args.to_session_key}`" if args.to_session_key else "(none)",
            ),
            ("reason", args.reason if args.reason else "(EMPTY)"),
            (
                "allow main session",
                _yes_no(args.allow_main_session),
            ),
        ],
    )

    # Parse-error branch.
    if args.parse_error:
        return _build_text(
            sections=[
                plan_section,
                (
                    "Apply",
                    [
                        ("status", "rejected"),
                        ("kind", "parse_error"),
                        ("reason", args.parse_error),
                    ],
                ),
            ],
        )

    # Validation parity with the TS parser (lcm-command.ts:499-507) —
    # surface missing required flags BEFORE hitting the operator body so
    # the operator gets a friendly message rather than a ReconcileError
    # with a generic kind.
    if not args.from_session_keys:
        return _build_text(
            sections=[
                plan_section,
                (
                    "Apply",
                    [
                        ("status", "rejected"),
                        ("kind", "missing_from"),
                        (
                            "fix",
                            "`/lcm reconcile-session-keys --apply` requires "
                            "`--from <comma-separated session_keys>`.",
                        ),
                    ],
                ),
            ],
        )
    if not args.to_session_key:
        return _build_text(
            sections=[
                plan_section,
                (
                    "Apply",
                    [
                        ("status", "rejected"),
                        ("kind", "missing_to"),
                        (
                            "fix",
                            "`/lcm reconcile-session-keys --apply` requires "
                            "`--to <destination session_key>`.",
                        ),
                    ],
                ),
            ],
        )
    if not args.reason or not args.reason.strip():
        return _build_text(
            sections=[
                plan_section,
                (
                    "Apply",
                    [
                        ("status", "rejected"),
                        ("kind", "missing_reason"),
                        (
                            "fix",
                            '`/lcm reconcile-session-keys --apply` requires `--reason "..."`.',
                        ),
                    ],
                ),
            ],
        )

    db = _resolve_db(parsed)
    if db is None:
        return _build_text(
            sections=[
                plan_section,
                (
                    "Apply",
                    [
                        ("status", "unavailable"),
                        (
                            "reason",
                            "engine DB connection not available (engine pre-init?)",
                        ),
                    ],
                ),
            ],
        )

    try:
        result = reconcile_session_keys(
            db,
            ReconcileArgs(
                from_session_keys=list(args.from_session_keys),
                to_session_key=args.to_session_key,
                reason=args.reason,
                allow_main_session=args.allow_main_session,
            ),
        )
    except ReconcileError as exc:
        return _build_text(
            sections=[
                plan_section,
                (
                    "Apply",
                    [
                        ("status", "failed"),
                        ("kind", exc.kind),
                        ("reason", str(exc)),
                    ],
                ),
            ],
        )
    except sqlite3.Error as exc:
        logger.exception("[reconcile] apply failed")
        return _build_text(
            sections=[
                plan_section,
                (
                    "Apply",
                    [
                        ("status", "failed"),
                        ("reason", str(exc)),
                    ],
                ),
            ],
        )

    from_label = ", ".join(f"`{k}`" for k in args.from_session_keys)
    summary_line = (
        f"Moved {result.conversations_moved} conversations, "
        f"{result.summaries_moved} summaries from {{{from_label}}} to "
        f"`{args.to_session_key}`"
    )
    return _build_text(
        sections=[
            plan_section,
            (
                "Apply",
                [
                    ("status", "completed"),
                    ("conversations moved", str(result.conversations_moved)),
                    ("summaries moved", str(result.summaries_moved)),
                    ("audit entries", str(result.audit_entries)),
                    ("summary", summary_line),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Parser — re-tokenize parsed.tokens / parsed.flags into _ApplyArgs
# ---------------------------------------------------------------------------


def _parse_apply_args(parsed: Any) -> _ApplyArgs:
    """Parse the parsed-command into a :class:`_ApplyArgs`.

    Ports the TS parser at ``lcm-command.ts:347-394`` (the body of
    ``parseReconcileArgs``). The router's :func:`_preparse_flags` already
    extracts ``--from`` (as a list), ``--to``, ``--reason``, and
    ``--allow-main-session`` into ``parsed.flags``. We re-read from
    ``parsed.tokens`` for unknown-flag detection and to surface
    parse errors operators recognize from the TS surface.

    Args:
        parsed: :class:`ParsedLcmCommand` with ``tokens`` (residual flag
            list after the canonical path was consumed) and ``flags``
            (router-level pre-parse).

    Returns:
        :class:`_ApplyArgs` with all fields filled in. On parse error
        (unknown flag), the ``parse_error`` field is set to the
        operator-facing message and parsing halts at that token.
    """
    flags = parsed.flags if hasattr(parsed, "flags") else {}
    tokens: list[str] = list(parsed.tokens) if hasattr(parsed, "tokens") else []

    from_session_keys: list[str] = list(flags.get("from", []) or [])
    to_session_key: str | None = flags.get("to")
    reason: str = flags.get("reason", "")
    allow_main_session: bool = bool(flags.get("allow_main_session", False))

    # Token-level unknown-flag check. The router consumed --apply,
    # --list-candidates, --allow-main-session, --from <v>, --to <v>,
    # --reason <v>; anything else is unknown. We walk the residual
    # tokens to detect bogus flags (e.g. `--from2 sk1`).
    parse_error: str | None = None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in (
            "--apply",
            "--list-candidates",
            "--allow-main-session",
        ):
            pass
        elif t in ("--from", "--to", "--reason"):
            # Skip the value token (router consumed it but tokens still
            # contains both).
            i += 1
        elif t == "":
            pass
        elif t.startswith("--"):
            parse_error = f"Unknown argument `{t}` for `/lcm reconcile-session-keys`."
            break
        else:
            # Bare positional argument — TS source rejects with the same
            # generic "Unknown argument" message.
            parse_error = f"Unknown argument `{t}` for `/lcm reconcile-session-keys`."
            break
        i += 1

    return _ApplyArgs(
        from_session_keys=from_session_keys,
        to_session_key=to_session_key,
        reason=reason,
        allow_main_session=allow_main_session,
        parse_error=parse_error,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_text(
    sections: Sequence[tuple[str, Sequence[tuple[str, str] | str]]],
) -> str:
    """Render the multi-section operator text block.

    Format mirrors the TS ``buildReconcileText`` / ``buildReconcileListText``
    output:

    ::

        Lossless Claw Reconcile Session Keys

        Section Name:
          key:    value
          key:    value
        ...

    Each section is a ``(heading, items)`` tuple. ``items`` may be a
    list of ``(key, value)`` tuples (rendered as ``  key: value``) or a
    list of plain strings (rendered as ``  string``). Mixing is
    supported (e.g. "Candidates" section is a plain-string list).

    Args:
        sections: ``[(heading, items), ...]``. ``items`` is either
            ``[(key, value), ...]`` or ``[str, ...]``.

    Returns:
        Single string with all sections joined by newlines.
    """
    lines: list[str] = ["Lossless Claw Reconcile Session Keys", ""]
    for heading, items in sections:
        lines.append(f"{heading}:")
        for item in items:
            if isinstance(item, tuple):
                key, value = item
                lines.append(f"  {key}: {value}")
            else:
                lines.append(f"  {item}")
        lines.append("")
    # Trim the trailing blank line for compactness.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _yes_no(b: bool) -> str:
    """``"yes"`` / ``"no"`` (TS parity: ``formatBoolean`` line 156 of
    ``lcm-command.ts``)."""
    return "yes" if b else "no"


def _resolve_db(parsed: Any) -> sqlite3.Connection | None:
    """Resolve the SQLite connection from ``parsed.engine``.

    The engine exposes its connection via different attributes depending
    on the engine state (Epic 02 noop engine vs Epic 03 wired engine).
    We probe both shapes — first the canonical ``db_connection``
    attribute, then the lower-level ``_conn`` attribute used by some
    test fixtures. Returns ``None`` on miss (operator gets a friendly
    "unavailable" text instead of an AttributeError stack trace).
    """
    engine = getattr(parsed, "engine", None)
    if engine is None:
        return None
    for attr in ("db_connection", "_db", "db", "_conn", "conn"):
        candidate = getattr(engine, attr, None)
        if isinstance(candidate, sqlite3.Connection):
            return candidate
    return None
