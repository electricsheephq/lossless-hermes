"""``/lcm doctor`` and ``/lcm doctor {apply,clean,clean apply}`` handlers.

* :func:`run_scan` — ``/lcm doctor`` (read-only scan). Stub until issue
  08-01's doctor-scan body lands.
* :func:`run_apply` — ``/lcm doctor apply`` (owner-gated, this issue
  08-07). Re-summarizes broken summaries in the current conversation by
  delegating to :func:`lossless_hermes.doctor.apply.apply_scoped_doctor_repair`.
* :func:`run_cleaners_scan` / :func:`run_cleaners_apply` — ``/lcm doctor
  clean`` + ``/lcm doctor clean apply``. Stubs until issue 08-08's
  cleaners body lands.

Maps to TS cases ``"doctor"`` and ``"doctor_cleaners"`` in
``lossless-claw/src/plugin/lcm-command.ts`` — the apply branch renders
the ``buildDoctorApplyText`` output (``lcm-command.ts:2474-2597``).

### Hermes-handler vs TS-plugin signature

The TS ``buildDoctorApplyText`` takes ``{ ctx, db, config, deps?,
summarize? }``. The Hermes slash-command hook signature is
``(raw_args) -> str | None`` — no per-call context object — so this
handler reads everything off ``parsed.engine`` (attached by
:class:`lossless_hermes.plugin.commands.LcmCommandDispatcher.handle`):

* ``db`` — the open :class:`sqlite3.Connection` (``engine._db``).
* ``config`` — the :class:`~lossless_hermes.db.config.LcmConfig`
  (``engine.config``).
* "current conversation" — resolved from ``engine.current_session_id``
  (the engine-tracked field that replaces TS ``ctx.sessionId``).
* ``deps`` / ``summarize`` — the summarizer seam. Probed off the engine
  if present; when the engine has not (yet) wired a summarizer surface,
  :func:`~lossless_hermes.doctor.apply.apply_scoped_doctor_repair`
  returns its ``"unavailable"`` arm and this handler renders that
  faithfully.

### Owner-gating per ADR-013

``/lcm doctor apply`` is owner-gated, but — like every ``/lcm``
destructive subcommand — the gate is **upstream** of dispatch
(Hermes's ``SlashAccessPolicy`` checks ``allow_admin_from`` before this
handler runs). This handler trusts authorization; the ``(admin)`` marker
in ``/lcm help`` documents the expected gate. ``/lcm doctor`` (without
``apply``) is read-only and ungated.

See:

* ``epics/08-cli-ops/08-07-doctor-apply.md`` — this issue.
* ``src/lossless_hermes/doctor/apply.py`` — the repair implementation.
* ``docs/adr/013-owner-gating.md`` — caller-side gating.
* ``lossless-claw/src/plugin/lcm-command.ts:2474-2597`` — TS source
  pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from lossless_hermes.doctor.apply import apply_scoped_doctor_repair
from lossless_hermes.doctor.contract import DoctorApplyResult
from lossless_hermes.doctor.shared import get_doctor_summary_stats

logger = logging.getLogger("lossless_hermes.commands.doctor")


# ---------------------------------------------------------------------------
# Render helpers (TS parity: buildHeaderLines / buildSection / buildStatLine)
# ---------------------------------------------------------------------------


def _build_section(title: str, lines: list[str]) -> str:
    """Render a section with two-space-indented body lines.

    TS parity: ``buildSection`` (``lcm-command.ts``). Matches the
    sibling :mod:`lossless_hermes.commands.status` renderer so all
    ``/lcm`` subcommands share one output style.
    """
    body = "\n".join(f"  {line}" for line in lines)
    return f"**{title}**\n{body}"


def _build_stat_line(label: str, value: str) -> str:
    """``"label: value"`` (TS parity: ``buildStatLine``)."""
    return f"{label}: {value}"


def _format_number(value: int) -> str:
    """Thousands-separated integer (TS parity: ``Intl.NumberFormat``)."""
    return f"{value:,}"


def _format_command(command: str) -> str:
    """Wrap in backticks (TS parity: ``formatCommand``)."""
    return f"`{command}`"


def _truncate_middle(value: str, max_chars: int) -> str:
    """Middle-ellipsize ``value`` to ``max_chars`` (TS parity: ``truncateMiddle``)."""
    if len(value) <= max_chars or max_chars <= 1:
        return value
    keep = max_chars - 1
    head = (keep + 1) // 2
    tail = keep // 2
    return f"{value[:head]}…{value[len(value) - tail :]}" if tail else f"{value[:head]}…"


# ---------------------------------------------------------------------------
# Engine resolution helpers
# ---------------------------------------------------------------------------


def _resolve_db(parsed: Any) -> Optional[sqlite3.Connection]:
    """Resolve the SQLite connection from ``parsed.engine``.

    Probes the canonical ``db_connection`` / ``_db`` attributes plus the
    lower-level names some test fixtures use. Returns :data:`None` on a
    miss (the handler then renders a friendly "unavailable" text rather
    than raising). Mirrors the resolver in
    :mod:`lossless_hermes.commands.purge`.
    """
    engine = getattr(parsed, "engine", None)
    if engine is None:
        return None
    for attr in ("db_connection", "_db", "db", "_conn", "conn"):
        candidate = getattr(engine, attr, None)
        if isinstance(candidate, sqlite3.Connection):
            return candidate
    return None


def _resolve_current_conversation_id(
    db: sqlite3.Connection, parsed: Any
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Resolve the current conversation for the doctor-apply scope.

    TS uses ``resolveCurrentConversation(ctx, db)`` — the equivalent
    here reads ``engine.current_session_id`` (set by
    :meth:`on_session_start`), then looks up the conversation row.

    Returns a ``(conversation_id, session_key, unavailable_reason)``
    triple:

    * On success — ``(id, session_key_or_None, None)``.
    * When no session is active — ``(None, None, "...")``. The reason
      string explains why the conversation-scoped repair could not run
      (no ``ctx`` in the Hermes hook → the engine must have seen an
      ``on_session_start`` first).
    """
    engine = getattr(parsed, "engine", None)
    session_id = getattr(engine, "current_session_id", None) if engine is not None else None
    if not session_id:
        return (
            None,
            None,
            "No active conversation — doctor apply is conversation-scoped "
            "and the engine has not seen a session yet.",
        )

    row = db.execute(
        """
        SELECT conversation_id, session_key
          FROM conversations
         WHERE session_id = ?
         ORDER BY active DESC, created_at DESC
         LIMIT 1
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        return (
            None,
            None,
            f"No conversation row found for the active session ({session_id}).",
        )
    conversation_id = int(row[0])
    session_key = str(row[1]) if row[1] is not None else None
    return (conversation_id, session_key, None)


# ---------------------------------------------------------------------------
# Public handlers — invoked by LcmCommandDispatcher
# ---------------------------------------------------------------------------


def run_scan(parsed: Any) -> str:  # noqa: ARG001 — stub (issue 08-01 owns the body)
    """``/lcm doctor`` (scan, read-only) stub."""
    return (
        "/lcm doctor: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )


def run_apply(parsed: Any) -> str:
    """``/lcm doctor apply`` — owner-gated per-conversation summary repair.

    Resolves the DB + config + current conversation off ``parsed.engine``,
    delegates to
    :func:`lossless_hermes.doctor.apply.apply_scoped_doctor_repair`, and
    renders the :class:`~lossless_hermes.doctor.contract.DoctorApplyResult`
    as the operator-facing text block. Ports the TS ``buildDoctorApplyText``
    renderer (``lcm-command.ts:2474-2597``).

    Owner-gating is upstream (ADR-013) — this handler trusts that the
    caller is authorized.

    Args:
        parsed: :class:`~lossless_hermes.plugin.commands.ParsedLcmCommand`.
            ``parsed.engine`` carries the engine; ``/lcm doctor apply``
            takes no flags, so ``parsed.tokens`` is unused.

    Returns:
        A multi-line operator-facing text block. Always non-empty; never
        raises out (a DB-unavailable / no-conversation / repair failure
        all render as a status section, never a stack trace).
    """
    header = _build_header_lines()

    db = _resolve_db(parsed)
    if db is None:
        return "\n".join([
            *header,
            "",
            "Lossless Hermes Doctor Apply",
            "",
            _build_section(
                "Current conversation",
                [
                    _build_stat_line("status", "unavailable"),
                    _build_stat_line(
                        "reason", "engine DB connection not available (engine pre-init?)"
                    ),
                ],
            ),
        ])

    conversation_id, session_key, unavailable_reason = _resolve_current_conversation_id(db, parsed)
    if conversation_id is None:
        return "\n".join([
            *header,
            "",
            "Lossless Hermes Doctor Apply",
            "",
            _build_section(
                "Current conversation",
                [
                    _build_stat_line("status", "unavailable"),
                    _build_stat_line("reason", unavailable_reason or "no active conversation"),
                    _build_stat_line(
                        "fallback",
                        "Doctor apply is conversation-scoped, so no global repair ran.",
                    ),
                ],
            ),
        ])

    engine = getattr(parsed, "engine", None)
    config = getattr(engine, "config", None)
    # The summarizer seam. The engine may expose `deps` / a `summarize`
    # callable once Epic 02/04 wiring lands; until then these probe to
    # None and apply_scoped_doctor_repair renders its "unavailable" arm.
    deps = getattr(engine, "deps", None)
    summarize = getattr(engine, "summarize", None)
    runtime_config = getattr(engine, "runtime_config", None)

    # Snapshot the doctor stats BEFORE the repair so the rendered
    # marker-kind counts reflect what was detected (the repair mutates
    # the rows; re-querying after would show fewer). Mirrors the TS
    # `getDoctorSummaryStats` call placed before `applyScopedDoctorRepair`
    # (lcm-command.ts:2497-2500).
    stats = get_doctor_summary_stats(db, conversation_id)

    conversation_lines = [
        _build_stat_line("conversation id", _format_number(conversation_id)),
        _build_stat_line(
            "session key",
            _format_command(_truncate_middle(session_key, 44)) if session_key else "missing",
        ),
        _build_stat_line("scope", "this conversation only"),
    ]

    try:
        result: DoctorApplyResult = apply_scoped_doctor_repair(
            db=db,
            config=config,
            conversation_id=conversation_id,
            deps=deps,
            summarize=summarize if callable(summarize) else None,
            runtime_config=runtime_config,
        )
    except Exception as error:  # noqa: BLE001 — handler must return text, not raise
        # apply_scoped_doctor_repair does not raise for per-target /
        # unavailable cases, but the final write transaction can
        # propagate a DB error. Render it as a `failed` section rather
        # than letting a stack trace escape the dispatcher. Mirrors the
        # TS `catch (error)` at lcm-command.ts:2508-2529.
        logger.warning("[lcm] doctor apply failed", exc_info=True)
        return "\n".join([
            *header,
            "",
            "Lossless Hermes Doctor Apply",
            "",
            _build_section("Current conversation", conversation_lines),
            "",
            _build_section(
                "Apply",
                [
                    _build_stat_line("mode", "in-place summary rewrite"),
                    _build_stat_line("status", "failed"),
                    _build_stat_line(
                        "reason", str(error) if str(error) else "unknown repair failure"
                    ),
                ],
            ),
        ])

    lines = [
        *header,
        "",
        "Lossless Hermes Doctor Apply",
        "",
        _build_section("Current conversation", conversation_lines),
        "",
    ]

    if result.kind == "unavailable":
        lines.append(
            _build_section(
                "Apply",
                [
                    _build_stat_line("mode", "in-place summary rewrite"),
                    _build_stat_line("status", "unavailable"),
                    _build_stat_line("reason", result.reason or "summarizer unavailable"),
                ],
            )
        )
        return "\n".join(lines)

    # Result rendering — mirrors lcm-command.ts:2558-2594.
    if stats.total == 0:
        result_text = "clean; no writes ran"
    elif result.repaired > 0:
        result_text = f"repaired {_format_number(result.repaired)} summary(s) in place"
    else:
        result_text = "no repairs applied"

    lines.append(
        _build_section(
            "Apply",
            [
                _build_stat_line("mode", "in-place summary rewrite"),
                _build_stat_line("detected summaries", _format_number(stats.total)),
                _build_stat_line("old-marker summaries", _format_number(stats.old)),
                _build_stat_line("truncated-marker summaries", _format_number(stats.truncated)),
                _build_stat_line("fallback-marker summaries", _format_number(stats.fallback)),
                _build_stat_line("repaired summaries", _format_number(result.repaired)),
                _build_stat_line("unchanged summaries", _format_number(result.unchanged)),
                _build_stat_line("skipped summaries", _format_number(len(result.skipped))),
                _build_stat_line("result", result_text),
            ],
        )
    )

    if result.repaired_summary_ids:
        lines.append("")
        lines.append(_build_section("Repaired summaries", [", ".join(result.repaired_summary_ids)]))

    if result.skipped:
        lines.append("")
        lines.append(
            _build_section(
                "Deferred",
                [f"{item['summary_id']}: {item['reason']}" for item in result.skipped],
            )
        )

    return "\n".join(lines)


def run_cleaners_scan(parsed: Any) -> str:  # noqa: ARG001 — stub (issue 08-08)
    """``/lcm doctor clean`` (owner-gated read-only) stub."""
    return (
        "/lcm doctor clean: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )


def run_cleaners_apply(parsed: Any) -> str:  # noqa: ARG001 — stub (issue 08-08)
    """``/lcm doctor clean apply`` (owner-gated destructive) stub."""
    return (
        "/lcm doctor clean apply: subcommand not yet implemented (Epic 08). "
        "Run /lcm help for the full subcommand inventory."
    )


# ---------------------------------------------------------------------------
# Header helper (placed last; depends on the package version)
# ---------------------------------------------------------------------------


def _build_header_lines() -> list[str]:
    """Render the two-line ``/lcm`` header (TS parity: ``buildHeaderLines``).

    Pulls the installed package version; on missing distribution
    metadata (dev trees / editable installs without a build) it falls
    back to ``"0.0.0"`` so the handler never crashes on a metadata
    lookup.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            pkg_version = version("lossless-hermes")
        except PackageNotFoundError:
            pkg_version = "0.0.0"
    except Exception:  # noqa: BLE001 — defensive; version is cosmetic
        pkg_version = "0.0.0"
    return [
        f"**Lossless Hermes v{pkg_version}**",
        f"Help: {_format_command('/lcm help')}",
    ]
