"""``/lcm health`` — v4.1 system-wide health probe.

Ports the TS ``buildHealthText`` from
``lossless-claw/src/plugin/lcm-command.ts`` (case ``"health"`` in
``parseLcmCommand``, ``buildHealthText`` at line 1714, plus
``formatHealthSnapshot`` at line 1627).

### Why this module exists

Issue 08-01 shipped a placeholder stub here; this issue (08-03)
replaces it with the full v4.1 health snapshot.

The handler renders five sections — embeddings, workers, synthesis,
eval, suppression — by:

1. Pulling a :class:`~lossless_hermes.operator.health.V41HealthSnapshot`
   via :func:`lossless_hermes.operator.health.get_v41_health_snapshot`.
   That call is pure-read-only and tolerant of missing tables (every
   probe returns sentinel values rather than raising).
2. Formatting the snapshot to plain-text-with-markdown sections per
   the TS ``formatHealthSnapshot`` shape — line-for-line modulo
   whitespace per the AC.

### Hermes-handler vs TS-plugin signature

TS ``buildHealthText`` takes ``{ db }``. Hermes's ``register_command``
hook receives only ``raw_args: str``; the dispatcher attaches
``engine`` via :attr:`parsed.engine` (see
:class:`~lossless_hermes.plugin.commands.LcmCommandDispatcher.handle`).

When the engine has not yet seen an ``on_session_start`` call (CLI
pre-first-message; gateway with no active conversation) ``engine._db``
is :data:`None`. The handler returns a clear "db not yet opened"
message instead of crashing — operators may type ``/lcm`` very early
in a debug session.

### What's deliberately NOT ported here

* The TS ``getWorkerStatusSnapshot`` integration (separate ``/lcm
  worker status`` subcommand at ``08-10``). Health surfaces just the
  per-job-kind lock state; the worker subcommand surfaces pending
  embedding/extraction queue counters and operational hints.
* The TS ``getDoctorSummaryStats`` integration — Doctor lands in
  Epic 08-05/06.
* The ``MAX_TOKENS_PER_EMBED_DOC`` value is read from
  :mod:`lossless_hermes.voyage.client` rather than hardcoded
  (Wave-4 Auditor #15 P1: TS comment line 187-190).

See:

* ``epics/08-cli-ops/08-03-health.md`` — this issue.
* ``docs/porting-guides/doctor-ops.md`` §"Operator modules" line 308
  — pure-read-only / tolerant-of-missing-tables contract.
* ``lossless-claw/src/plugin/lcm-command.ts:1714-1724`` and
  ``:1627-1712`` — TS source pinned at commit ``1f07fbd``.
* ``lossless-claw/src/operator/health.ts:1-442`` — TS snapshot source.
"""

from __future__ import annotations

import logging
from typing import Any

from lossless_hermes.commands.status import (
    _build_header_lines,
    _build_section,
    _build_stat_line,
    _format_number,
)
from lossless_hermes.operator.health import (
    EmbeddingsHealth,
    EvalHealth,
    SuppressionHealth,
    SynthesisHealth,
    V41HealthSnapshot,
    WorkerStatus,
    get_v41_health_snapshot,
)
from lossless_hermes.voyage.client import MAX_TOKENS_PER_EMBED_DOC

logger = logging.getLogger("lossless_hermes.commands.health")


# ---------------------------------------------------------------------------
# Formatting helpers — narrow ports of the TS helpers in lcm-command.ts
# ---------------------------------------------------------------------------


def _format_worker_line(worker: WorkerStatus) -> str:
    """Render one worker status line.

    Ports ``lcm-command.ts:1616-1625`` ``formatWorkerLine``. Output:

    * Idle (no lock held): ``"<job_kind>: (idle)"``.
    * Active: ``"<job_kind>: worker_id=<id>[ EXPIRED] acquired_at=<ts>
      expires_at=<ts>"``. The ``" EXPIRED"`` marker appears
      immediately after ``worker_id`` (no separator) — matches the TS
      source verbatim.
    """
    if not worker.active:
        return f"{worker.job_kind}: (idle)"
    expired_flag = " EXPIRED" if worker.expired else ""
    worker_id = worker.worker_id if worker.worker_id is not None else "unknown"
    acquired = worker.acquired_at if worker.acquired_at is not None else "unknown"
    expires = worker.expires_at if worker.expires_at is not None else "unknown"
    return (
        f"{worker.job_kind}: worker_id={worker_id}{expired_flag} "
        f"acquired_at={acquired} expires_at={expires}"
    )


def _format_embeddings_section(embeddings: EmbeddingsHealth) -> str:
    """Render the ``Embeddings`` section.

    Ports the TS embeddings block at ``lcm-command.ts:1631-1658``.
    """
    lines: list[str] = []
    if embeddings.active_profile is not None:
        p = embeddings.active_profile
        lines.append(
            _build_stat_line(
                "active model",
                f"{p.model_name} (dim={_format_number(p.dim)})",
            )
        )
    else:
        lines.append(_build_stat_line("active model", "NOT REGISTERED"))
    lines.append(
        _build_stat_line(
            "vec0 status",
            embeddings.vec0_version if embeddings.vec0_version is not None else "NOT LOADED",
        )
    )
    lines.append(
        _build_stat_line(
            "pending backfill",
            f"{_format_number(embeddings.pending_backfill)} docs",
        )
    )
    lines.append(_build_stat_line("embedded count", _format_number(embeddings.embedded_count)))
    # LCM Wave-4 Auditor #15 P1 (2026-05-14): surface over-cap leaves
    # so "pending=0" doesn't lie about coverage. See operator/health.py
    # for the SQL details.
    if embeddings.over_cap_pending > 0:
        lines.append(
            _build_stat_line(
                f"over-cap leaves (>{_format_number(MAX_TOKENS_PER_EMBED_DOC)} tokens, "
                "NOT embeddable)",
                f"{_format_number(embeddings.over_cap_pending)} — re-summarize at "
                "lower cap to bring into range",
            )
        )
    return _build_section("Embeddings", lines)


def _format_workers_section(workers: tuple[WorkerStatus, ...]) -> str:
    """Render the ``Workers`` section.

    Ports the TS workers block at ``lcm-command.ts:1662``. One line per
    job kind via :func:`_format_worker_line`.
    """
    return _build_section("Workers", [_format_worker_line(w) for w in workers])


def _format_synthesis_section(synthesis: SynthesisHealth) -> str:
    """Render the ``Synthesis`` section.

    Ports the TS synthesis block at ``lcm-command.ts:1666-1677``.
    """
    return _build_section(
        "Synthesis",
        [
            _build_stat_line(
                "active prompts",
                f"{_format_number(synthesis.active_prompt_count)} across "
                f"{_format_number(synthesis.distinct_memory_type_count)} memory_types",
            ),
            _build_stat_line(
                "recent synthesis runs",
                f"{_format_number(synthesis.recent_synthesis_runs_7d)} (last 7 days)",
            ),
        ],
    )


def _format_eval_section(eval_health: EvalHealth) -> str:
    """Render the ``Eval`` section.

    Ports the TS eval block at ``lcm-command.ts:1681-1701``.
    """
    lines: list[str] = [
        _build_stat_line(
            "query sets registered",
            _format_number(eval_health.query_set_count),
        ),
    ]
    if eval_health.most_recent_run is not None:
        r = eval_health.most_recent_run
        lines.append(
            _build_stat_line(
                "most-recent run",
                f"{r.query_set_id} mode={r.mode} recall={r.recall_score:.3f} (run_id={r.run_id})",
            )
        )
    else:
        lines.append(_build_stat_line("most-recent run", "(none)"))
    if eval_health.drift_index is None:
        lines.append(_build_stat_line("drift index", "(no baseline)"))
    else:
        # TS source: `sign = drift >= 0 ? "+" : ""` then `${sign}${value.toFixed(4)}`.
        # `value.toFixed(4)` already includes the minus sign for negatives,
        # so we only prepend "+" for non-negative values.
        sign = "+" if eval_health.drift_index >= 0 else ""
        lines.append(_build_stat_line("drift index", f"{sign}{eval_health.drift_index:.4f}"))
    return _build_section("Eval", lines)


def _format_suppression_section(suppression: SuppressionHealth) -> str:
    """Render the ``Suppression`` section.

    Ports the TS suppression block at ``lcm-command.ts:1705-1709``.
    """
    return _build_section(
        "Suppression",
        [
            _build_stat_line(
                "suppressed leaves",
                _format_number(suppression.suppressed_leaves),
            ),
        ],
    )


def format_v41_health_snapshot(snapshot: V41HealthSnapshot) -> list[str]:
    """Format a v4.1 health snapshot as a list of section strings.

    Ports ``lcm-command.ts:1627-1712`` ``formatHealthSnapshot``. The
    output is a list (joined by ``"\\n"`` by the caller); each
    section is separated from the next by an empty-string entry so
    the rendered output has blank lines between sections (matches TS
    line-for-line modulo whitespace).

    Args:
        snapshot: A :class:`V41HealthSnapshot` (typically from
            :func:`lossless_hermes.operator.health.get_v41_health_snapshot`).

    Returns:
        Ordered list of strings ready to be joined with ``"\\n"``.
    """
    lines: list[str] = [
        _format_embeddings_section(snapshot.embeddings),
        "",
        _format_workers_section(snapshot.workers),
        "",
        _format_synthesis_section(snapshot.synthesis),
        "",
        _format_eval_section(snapshot.eval),
        "",
        _format_suppression_section(snapshot.suppression),
    ]
    return lines


# ---------------------------------------------------------------------------
# Public entry point — dispatcher routes ``/lcm health`` here
# ---------------------------------------------------------------------------


def run(parsed: Any) -> str:
    """Render ``/lcm health``.

    Reads from :attr:`engine._db` (open ``sqlite3.Connection``) via the
    snapshot helper. When the engine is uninitialized (``_db is None``,
    e.g. the dispatcher is invoked before any Hermes session start),
    returns a graceful "engine not yet initialized" message rather
    than crashing — operators may type ``/lcm`` very early in a debug
    session.

    Per the issue 08-03 spec acceptance criteria:

    * "Every probe handles missing tables via :func:`has_table`
      guard, never raises." — implemented as ``try / except
      sqlite3.Error`` per probe in
      :mod:`lossless_hermes.operator.health`.
    * "``format_v41_health_snapshot`` matches TS ``formatV41HealthSnapshot``
      line-for-line modulo whitespace." — see the section helpers above.
    * "Workers probe shows held-by host, age, heartbeat status." —
      ``_format_worker_line`` emits ``worker_id``, ``acquired_at``,
      ``expires_at``; expired locks are flagged.

    Args:
        parsed: The :class:`ParsedLcmCommand`. Reads ``parsed.engine``
            — set by the dispatcher before invoking.

    Returns:
        Multi-line markdown string ready for chat rendering. On any
        unexpected error (DB read failure, missing engine attribute),
        the body logs the exception and returns a one-line ``"health
        failed: <reason>"`` string — never raises.
    """
    engine = getattr(parsed, "engine", None)
    if engine is None:
        logger.warning("[lcm] /lcm health invoked with no engine on parsed")
        return "/lcm health: dispatcher misconfigured (no engine reference)."

    db = getattr(engine, "_db", None)
    lines: list[str] = list(_build_header_lines())
    lines.append("")
    lines.append("### v4.1 Health")
    lines.append("")

    if db is None:
        # Engine constructed but on_session_start has not yet run.
        # Append a clarifying "DB not yet open" footer instead of
        # rendering an all-zeros snapshot.
        lines.append(
            _build_section(
                "Status",
                [
                    _build_stat_line("db", "not yet opened"),
                    _build_stat_line(
                        "hint",
                        "Send at least one message to trigger on_session_start.",
                    ),
                ],
            )
        )
        return "\n".join(lines)

    try:
        snapshot = get_v41_health_snapshot(db)
    except Exception as exc:  # noqa: BLE001 — operator-facing diagnostic
        # Per the AC: probes are tolerant of missing tables — they
        # return sentinel values rather than raising. Anything that
        # DOES raise is a programmer error (missing DB connection
        # method, etc.); surface it as a one-line failure rather than
        # crashing the chat session.
        logger.exception("[lcm] /lcm health: snapshot query failed")
        return f"/lcm health failed: {exc!s}"

    lines.extend(format_v41_health_snapshot(snapshot))
    return "\n".join(lines)
