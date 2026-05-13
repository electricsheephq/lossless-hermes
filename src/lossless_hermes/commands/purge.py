"""``/lcm purge`` — soft-suppression handler (Epic 08-04).

Replaces the Epic 08-01 stub with a real handler that parses the
``/lcm purge --reason ... [scope-flag(s)] [--apply | --allow-main-session]``
surface, delegates to :func:`lossless_hermes.operator.purge.run_purge` /
:func:`lossless_hermes.operator.purge.preview_purge_affected`, and
formats the result as the operator-facing text block.

Ports the TS ``case "purge"`` parser at
``lossless-claw/src/plugin/lcm-command.ts:573-694`` plus the
``buildPurgeText`` renderer at lines 2188-2358.

### CLI surface (per ``plugin-glue.md`` "/lcm slash commands" line 438)

::

    /lcm purge --reason "..."
      [--session-key <k>]
      [--summary-ids id1,id2,id3]
      [--since <iso>]
      [--before <iso>]
      [--min-token-count <n>]
      [--allow-main-session]
      [--apply]

* ``--reason`` is **required** (quoted free text).
* At least one scope criterion must be specified
  (``--session-key``, ``--summary-ids``, ``--since``, ``--before``, or
  ``--min-token-count``).
* ``--allow-main-session`` is required to target ``agent:main:main``
  (Eva's primary thread).
* ``--apply`` commits the cascade. Default is dry-run (counts only;
  no writes).

### Output shape

Mirrors the TS ``buildPurgeText`` output as plain text (the Hermes
``register_command`` contract returns ``str``). Three sections:

* Header — fixed prefix lines.
* Criteria — echo back what the parser saw (so operators can spot
  typos before ``--apply``).
* Outcome — for dry-run: ``would-affect-leaves`` count + reminder to
  re-run with ``--apply``. For ``--apply``: ``affected leaves`` count
  + ``purge session id``.

### DB access

The handler reads ``parsed.engine`` (set by
:class:`lossless_hermes.plugin.commands.LcmCommandDispatcher.handle`)
and obtains the SQLite connection from the engine. When the engine has
no DB connection (misconfiguration or pre-init), returns a friendly
"DB unavailable" text and does NOT raise.

### Owner-gating per ADR-013

Owner-gating is **upstream** — Hermes's ``SlashAccessPolicy`` gates the
``allow_admin_from`` config before this handler runs. The handler does
NOT check ``is_owner`` itself. The ``(admin)`` marker in ``/lcm help``
documents the expected gate.

See:

* ``epics/08-cli-ops/08-04-purge-soft-suppression.md`` — this issue.
* ``src/lossless_hermes/operator/purge.py`` — the cascade implementation.
* ``docs/adr/013-owner-gating.md`` — caller-side gating, not
  handler-side.
* ``lossless-claw/src/plugin/lcm-command.ts:573-694, 2188-2358`` — TS
  source pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from lossless_hermes.operator.purge import (
    PurgeCriteria,
    PurgeError,
    PurgeOptions,
    preview_purge_affected,
    run_purge,
)

logger = logging.getLogger("lossless_hermes.commands.purge")


# ---------------------------------------------------------------------------
# Parsed-args dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PurgeArgs:
    """Internal result of :func:`_parse_purge_args`.

    Pre-validation per-flag values. The handler combines these into a
    :class:`PurgeOptions` after calling validation; the operator-facing
    error path is "echo back what the parser saw + the reason it
    rejected" rather than crashing with a stack trace.
    """

    reason: str = ""
    session_key: str | None = None
    summary_ids: list[str] | None = None
    since: datetime | None = None
    before: datetime | None = None
    min_token_count: int | None = None
    allow_main_session: bool = False
    apply: bool = False
    parse_error: str | None = None


# ---------------------------------------------------------------------------
# Public handler — invoked by LcmCommandDispatcher
# ---------------------------------------------------------------------------


def run(parsed: Any) -> str:
    """``/lcm purge`` handler — dry-run by default, ``--apply`` commits.

    The dispatcher calls this with a :class:`ParsedLcmCommand` whose
    ``tokens`` is the residual after the ``"purge"`` canonical-path was
    consumed (i.e. the flag list). We re-tokenize via the parser below
    to honor the per-subcommand flag semantics (TS source does the same;
    see ``lcm-command.ts:577-679``).

    Args:
        parsed: :class:`ParsedLcmCommand`. ``parsed.tokens`` is the flag
            list; ``parsed.engine`` is the :class:`LCMEngine` instance.

    Returns:
        Multi-line operator-facing text block. Always non-empty; never
        raises (per the dispatcher's "be robust" contract — any internal
        crash becomes a ``failed`` outcome line).
    """
    args = _parse_purge_args(parsed.tokens)

    header = [
        "Lossless Claw Purge (soft mode)",
        "",
    ]
    criteria_lines = [
        "Criteria:",
        f"  session-key:     {args.session_key if args.session_key else '(none)'}",
        f"  summary-ids:     {f'{len(args.summary_ids)} ids' if args.summary_ids else '(none)'}",
        f"  since:           {args.since.isoformat() if args.since else '(none)'}",
        f"  before:          {args.before.isoformat() if args.before else '(none)'}",
        f"  min-token-count: "
        f"{args.min_token_count if args.min_token_count is not None else '(none)'}",
        f"  reason:          {args.reason if args.reason else '(EMPTY)'}",
        f"  allow main session: {_yes_no(args.allow_main_session)}",
        f"  apply:           {_yes_no(args.apply)}",
        "",
    ]

    # Parse-error branch: render the criteria echo + the parse error.
    if args.parse_error:
        return "\n".join([
            *header,
            *criteria_lines,
            "Outcome:",
            "  status:  rejected",
            "  kind:    parse_error",
            f"  reason:  {args.parse_error}",
        ])

    # Missing-reason short-circuit — mirrors TS buildPurgeText lines 2228-2240.
    if not args.reason or not args.reason.strip():
        return "\n".join([
            *header,
            *criteria_lines,
            "Outcome:",
            "  status:  rejected",
            "  kind:    missing_reason",
            '  fix:     Pass `--reason "free text describing why this is being purged"`',
        ])

    # Resolve DB from the engine. The dispatcher attaches `engine` to
    # `parsed` (see plugin/commands.py:473). If the engine has no DB
    # (misconfiguration / pre-init), return a friendly text — do NOT
    # raise; the dispatcher trusts handlers to return strings.
    db = _resolve_db(parsed)
    if db is None:
        return "\n".join([
            *header,
            *criteria_lines,
            "Outcome:",
            "  status:  unavailable",
            "  reason:  engine DB connection not available (engine pre-init?)",
        ])

    criteria = PurgeCriteria(
        summary_ids=args.summary_ids,
        session_key=args.session_key,
        since=args.since,
        before=args.before,
        min_token_count=args.min_token_count,
    )

    # Dry-run path (default): preview count, do NOT modify DB.
    # Mirrors TS buildPurgeText lines 2267-2326.
    if not args.apply:
        # Mirror runPurge's no-criteria validation so we don't return a
        # misleading whole-DB count for an empty-criteria preview.
        # (TS BUG-5 regression guard preserved.)
        has_criteria = bool(
            (criteria.summary_ids and len(criteria.summary_ids) > 0)
            or criteria.session_key
            or criteria.since
            or criteria.before
            or criteria.min_token_count is not None
        )
        if not has_criteria:
            return "\n".join([
                *header,
                *criteria_lines,
                "Preview:",
                "  status:  rejected",
                "  kind:    no_criteria",
                "  fix:     Pass at least one of: --session-key, --summary-ids, "
                "--since, --before, --min-token-count",
            ])
        try:
            preview_count = preview_purge_affected(db, criteria)
        except sqlite3.Error as exc:
            logger.exception("[purge] preview failed")
            return "\n".join([
                *header,
                *criteria_lines,
                "Outcome:",
                "  status:  preview_failed",
                f"  reason:  {exc!s}",
            ])
        warning_lines: list[str] = []
        if args.session_key == "agent:main:main" and not args.allow_main_session:
            warning_lines.append(
                "  warning: --session-key=agent:main:main without --allow-main-session — "
                "apply WILL be blocked. Pass --allow-main-session to unblock."
            )
        return "\n".join([
            *header,
            *criteria_lines,
            "Preview:",
            f"  would-affect-leaves: {preview_count:,}",
            "  to apply:            Re-run with the same flags plus `--apply` "
            "to actually suppress.",
            "  race window:         Preview is best-effort; --apply re-resolves "
            "under transaction. Counts may differ if leaves are written or "
            "suppressed in the meantime.",
            *warning_lines,
        ])

    # --apply path.
    opts = PurgeOptions(
        reason=args.reason,
        criteria=criteria,
        allow_main_session=args.allow_main_session,
    )
    try:
        result = run_purge(db, opts)
    except PurgeError as exc:
        return "\n".join([
            *header,
            *criteria_lines,
            "Apply:",
            "  status:  failed",
            f"  kind:    {exc.kind}",
            f"  reason:  {exc!s}",
        ])
    except sqlite3.Error as exc:
        logger.exception("[purge] apply failed")
        return "\n".join([
            *header,
            *criteria_lines,
            "Apply:",
            "  status:  failed",
            f"  reason:  {exc!s}",
        ])
    return "\n".join([
        *header,
        *criteria_lines,
        "Apply:",
        "  status:           completed",
        f"  mode:             {result.mode}",
        f"  affected leaves:  {len(result.affected_leaf_ids):,}",
        f"  purge session id: {result.purge_session_id}",
    ])


# ---------------------------------------------------------------------------
# Parser — re-tokenize parsed.tokens into _PurgeArgs
# ---------------------------------------------------------------------------


def _parse_purge_args(tokens: list[str]) -> _PurgeArgs:
    """Parse the residual flag tokens into a :class:`_PurgeArgs`.

    Ports the TS parser at ``lcm-command.ts:577-679`` (the body of
    ``case "purge"``). The dispatcher pre-parses ``--reason``,
    ``--apply``, and ``--allow-main-session`` into ``parsed.flags``, but
    we re-tokenize here from ``parsed.tokens`` because:

    * ``--summary-ids``, ``--since``, ``--before``, ``--min-token-count``
      need per-subcommand parsing (comma-list, ISO timestamp, integer).
    * Unknown flags / bare positional args need per-subcommand error
      messages (TS Wave-12 reviewer P2 fix: ``purge sk1`` → "Did you
      mean ``--session-key sk1``?").

    Returns:
        :class:`_PurgeArgs` with all fields filled in. On parse error
        (unknown flag, missing flag value, bad ISO timestamp), the
        ``parse_error`` field is set to the operator-facing message and
        parsing halts at that token.
    """
    reason = ""
    session_key: str | None = None
    summary_ids: list[str] | None = None
    since: datetime | None = None
    before: datetime | None = None
    min_token_count: int | None = None
    allow_main_session = False
    apply = False
    parse_error: str | None = None

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--apply":
            apply = True
        elif t == "--allow-main-session":
            allow_main_session = True
        elif t == "--reason":
            i += 1
            if i >= len(tokens):
                parse_error = "`--reason` requires a value (in quotes if multi-word)."
                break
            reason = tokens[i]
        elif t == "--session-key":
            i += 1
            if i >= len(tokens):
                parse_error = "`--session-key` requires a value."
                break
            session_key = tokens[i]
        elif t == "--summary-ids":
            i += 1
            if i >= len(tokens):
                parse_error = "`--summary-ids` requires a comma-separated list."
                break
            summary_ids = [s.strip() for s in tokens[i].split(",") if s.strip()]
        elif t == "--since":
            i += 1
            if i >= len(tokens):
                parse_error = "`--since` requires an ISO timestamp."
                break
            since = _parse_iso(tokens[i])
            if since is None:
                parse_error = f"`--since` value `{tokens[i]}` is not a valid ISO timestamp."
                break
        elif t == "--before":
            i += 1
            if i >= len(tokens):
                parse_error = "`--before` requires an ISO timestamp."
                break
            before = _parse_iso(tokens[i])
            if before is None:
                parse_error = f"`--before` value `{tokens[i]}` is not a valid ISO timestamp."
                break
        elif t == "--min-token-count":
            i += 1
            if i >= len(tokens):
                parse_error = "`--min-token-count` requires a non-negative integer."
                break
            try:
                n = int(tokens[i])
                if n < 0:
                    raise ValueError(tokens[i])
                min_token_count = n
            except (TypeError, ValueError):
                parse_error = "`--min-token-count` requires a non-negative integer."
                break
        elif t == "":
            pass
        elif t.startswith("--"):
            parse_error = f"Unknown flag for `/lcm purge`: {t}"
            break
        else:
            # LCM Wave-12 reviewer P2 fix preserved from TS: bare
            # positional args (e.g. `purge sk1` instead of `purge
            # --session-key sk1`) were silently swallowed pre-fix and
            # the command ran with no scope. Now we reject explicitly.
            # Original: lossless-claw/src/plugin/lcm-command.ts:665-676.
            parse_error = (
                f"Unexpected positional arg for `/lcm purge`: `{t}`. "
                f"Did you mean `--session-key {t}`? Use `/lcm help` for usage."
            )
            break
        i += 1

    return _PurgeArgs(
        reason=reason,
        session_key=session_key,
        summary_ids=summary_ids,
        since=since,
        before=before,
        min_token_count=min_token_count,
        allow_main_session=allow_main_session,
        apply=apply,
        parse_error=parse_error,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string. Returns ``None`` on failure.

    Mirrors the TS ``new Date(v)`` check (lcm-command.ts:632-636,
    646-650). Python's :py:meth:`datetime.fromisoformat` accepts a wider
    range of formats than the JS ``Date`` constructor as of Python 3.11,
    so most operator inputs that worked in TS will also work here.
    """
    try:
        # fromisoformat accepts trailing 'Z' as of Python 3.11 — but
        # older Pythons reject it. Be defensive.
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return None


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
