"""``/lcm worker`` — worker status snapshot + forced tick (Epic 08-17).

Replaces the Epic 08-01 router stub with the real bodies for the two
worker subcommands that share a parent dispatch:

* ``/lcm worker`` / ``/lcm worker status`` — read-only worker status
  snapshot. **NOT owner-gated** — anyone can introspect lock state.
* ``/lcm worker tick embedding-backfill`` — force one tick of the
  embedding-backfill worker. **OWNER-GATED** (per ``plugin-glue.md``
  line 430: "200 paid Voyage embeddings per call. Owner-gated because
  of paid quota burn.").

Both delegate to :mod:`lossless_hermes.operator.worker_orchestrator`
(issue 08-10): :func:`~lossless_hermes.operator.worker_orchestrator.get_worker_status_snapshot`
and :func:`~lossless_hermes.operator.worker_orchestrator.tick_embedding_backfill`.

Ports the TS ``case "worker"`` body at
``lossless-claw/src/plugin/lcm-command.ts:516-546`` plus the
``buildWorkerStatusText`` renderer at lines 1726-1765 and the
``buildWorkerTickBackfillText`` renderer at lines 1778-1897.

### Parent dispatch (why ``run_status`` branches on ``tokens``)

The router (08-01) does longest-prefix matching against the canonical
subcommand paths. Three worker paths are registered:

* ``worker`` → ``run_status``
* ``worker status`` → ``run_status``
* ``worker tick embedding-backfill`` → ``run_tick_backfill``

An input like ``/lcm worker tick foo`` matches only the 1-token
``worker`` path (the 3-token ``worker tick embedding-backfill`` path
fails on the third token), so it lands in :func:`run_status` with
``parsed.tokens == ["tick", "foo"]``. Likewise ``/lcm worker tick``
(no kind) lands in :func:`run_status` with ``parsed.tokens ==
["tick"]``. :func:`run_status` is therefore the **parent dispatch**: it
inspects ``parsed.tokens`` and routes the ``tick`` cases to the tick
handler — mirroring the TS ``case "worker"`` sub-switch.

### Future tick kinds (``_TICK_KINDS`` registry)

The TS source wires only ``worker tick embedding-backfill``. Per the
issue spec the Python port drives the tick dispatch from
:data:`_TICK_KINDS` so additional kinds (e.g.
``worker tick entity-extraction``) can be added without touching the
08-01 router — a new entry in :data:`_TICK_KINDS` + a new canonical
path in ``plugin/commands.py`` is all it takes.

### Owner-gating per ADR-013

Owner-gating is **upstream** — Hermes's ``SlashAccessPolicy`` gates the
``allow_admin_from`` config BEFORE this handler runs. The handler does
NOT check owner status itself (the AC mandates
``grep -n "is_owner" worker.py`` returns 0 lines). The dispatcher table
(08-01) marks ``worker tick embedding-backfill`` as ``owner_gated=True``;
that flag drives the upstream gate and the ``(admin)`` marker in
``/lcm help``. ``worker`` / ``worker status`` are NOT gated.

See:

* ``epics/08-cli-ops/08-17-worker-status.md`` — this issue.
* ``src/lossless_hermes/operator/worker_orchestrator.py`` — the 08-10
  orchestrator surfaces this handler delegates to.
* ``docs/adr/013-owner-gating.md`` — caller-side gating, not
  handler-side.
* ``lossless-claw/src/plugin/lcm-command.ts:516-546, 1616-1625,
  1726-1765, 1778-1897`` — TS source pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from typing import Any, Final

from lossless_hermes.commands.status import _build_header_lines
from lossless_hermes.concurrency.model import WORKER_JOB_KINDS
from lossless_hermes.embeddings.semantic_search import get_active_embedding_model
from lossless_hermes.operator.worker_orchestrator import (
    WorkerStatusSnapshot,
    get_worker_status_snapshot,
    tick_embedding_backfill,
)

logger = logging.getLogger("lossless_hermes.commands.worker")


# ---------------------------------------------------------------------------
# Tick-kind registry — drives /lcm worker tick <kind> dispatch
# ---------------------------------------------------------------------------
#
# The TS source wires only ``embedding-backfill``. Per the issue spec
# (line 82) the dispatch is registry-driven so future kinds can be added
# without code changes to the 08-01 router. Adding a kind requires:
#   1. an entry here (the operator-visible kind name), AND
#   2. a canonical path in ``plugin/commands.py`` routing to the right
#      ``run_tick_*`` handler.
# The set is kept as a tuple (ordered) so the "valid kinds" error message
# is deterministic.
_TICK_KINDS: Final[tuple[str, ...]] = ("embedding-backfill",)


# ---------------------------------------------------------------------------
# Public handlers — invoked by LcmCommandDispatcher
# ---------------------------------------------------------------------------


def run_status(parsed: Any) -> str:
    """``/lcm worker`` / ``/lcm worker status`` — parent dispatch + status.

    This is the **parent dispatch** for the ``worker`` subcommand tree
    (see the module docstring). It inspects ``parsed.tokens``:

    * ``[]`` or ``["status"]`` → render the read-only status snapshot.
    * ``["tick"]`` → ``worker tick`` with no kind → help-style error
      naming the valid kinds (TS parity, ``lcm-command.ts:523-530``).
    * ``["tick", "<kind>"]`` where ``<kind>`` is unknown → unknown-kind
      error (AC line 94).
    * ``["tick", "embedding-backfill", ...]`` → delegate to
      :func:`run_tick_backfill`. (Normally the router resolves this to
      :func:`run_tick_backfill` directly via the 3-token canonical path;
      this branch is the defensive fallback so the parent dispatch is
      self-consistent.)
    * anything else → help-style error (TS parity,
      ``lcm-command.ts:542-545``).

    **NOT owner-gated.** Status is read-only — non-owner sessions can
    introspect lock state (TS parity: ``lcm-command.test.ts`` "allows
    /lcm worker status (read-only) when sender is not owner").

    Args:
        parsed: :class:`~lossless_hermes.plugin.commands.ParsedLcmCommand`.
            ``parsed.tokens`` is the residual token list after the
            ``worker`` canonical path was consumed; ``parsed.engine`` is
            the :class:`LCMEngine` instance attached by the dispatcher.

    Returns:
        Multi-line operator-facing text block. Always non-empty; never
        raises (per the dispatcher's "be robust" contract — any internal
        crash becomes a one-line failure rather than a stack trace).
    """
    tokens: list[str] = [str(t) for t in getattr(parsed, "tokens", []) or []]
    lowered = [t.lower() for t in tokens]

    # Parent dispatch — route the ``tick`` sub-cases. Mirrors the TS
    # ``case "worker"`` sub-switch at lcm-command.ts:518-545.
    if lowered and lowered[0] == "tick":
        kind = lowered[1] if len(lowered) > 1 else None
        if not kind:
            # `/lcm worker tick` with no kind — TS lcm-command.ts:523-530.
            return _build_tick_error(
                f"`/lcm worker tick` requires a job kind. Valid kinds: {', '.join(_TICK_KINDS)}"
            )
        if kind not in _TICK_KINDS:
            # AC line 94: unknown kind → exact wording.
            return _build_tick_error(
                f"unknown kind '{kind}'. Valid kinds: {', '.join(_TICK_KINDS)}"
            )
        # Known kind reached run_status (rather than the dedicated
        # 3-token canonical route). Delegate so behaviour is identical.
        return run_tick_backfill(parsed)

    # Bare `/lcm worker` or `/lcm worker status` → render the snapshot.
    # Anything else (e.g. `/lcm worker bogus`) → help-style error,
    # matching TS lcm-command.ts:542-545.
    if lowered and lowered[0] != "status":
        return _build_tick_error(
            f"`/lcm worker` accepts `status` (default) or `tick <kind>`. Got: `{tokens[0]}`"
        )

    return _render_status(parsed)


def run_tick_backfill(parsed: Any) -> str:
    """``/lcm worker tick embedding-backfill`` — force one backfill tick.

    **OWNER-GATED** (upstream, per ADR-013 — this handler does NOT
    re-check). Burns up to 200 paid Voyage embeddings per call
    (``per_tick_limit=200`` default in
    :mod:`lossless_hermes.embeddings.backfill`).

    Synchronous-bridge handler: the dispatcher's
    :meth:`~lossless_hermes.plugin.commands.LcmCommandDispatcher.handle`
    is sync, but
    :func:`~lossless_hermes.operator.worker_orchestrator.tick_embedding_backfill`
    is ``async`` (it awaits Voyage HTTP). We bridge via
    :func:`asyncio.run` on a fresh event loop — the same pattern used by
    :mod:`lossless_hermes.tools.grep` and
    :mod:`lossless_hermes.tools.synthesize_around` per ADR-017
    (sync-by-design tool/command surface).

    Renders one of three outcome variants (TS parity,
    ``buildWorkerTickBackfillText`` at ``lcm-command.ts:1778-1897``):

    * **Processed** — ``embedded_count`` embeddings written; reports
      Voyage token spend, estimated cost, remaining queue, tick latency.
    * **Skipped (empty queue)** — no unembedded leaves; nothing to do.
    * **Skipped (lock held)** — a peer worker holds the
      ``embedding-backfill`` lock; this tick was a no-op.

    Args:
        parsed: :class:`~lossless_hermes.plugin.commands.ParsedLcmCommand`.
            ``parsed.engine`` carries the engine; the subcommand takes no
            flags so ``parsed.tokens`` is unused for the canonical route.

    Returns:
        A multi-line operator-facing text block. Always non-empty; never
        raises out (DB-unavailable / pre-flight failure / tick error all
        render as a status section).
    """
    title = "[lcm] worker tick embedding-backfill"

    db = _resolve_db(parsed)
    if db is None:
        return "\n".join([
            title,
            "Skipped: engine DB connection not available (engine pre-init?).",
        ])

    # Pre-flight 1: VOYAGE_API_KEY must be set (the tick makes paid HTTP
    # calls). Mirrors TS lcm-command.ts:1782-1787.
    api_key = os.environ.get("VOYAGE_API_KEY", "").strip()
    if not api_key:
        return "\n".join([
            title,
            "Skipped: VOYAGE_API_KEY env var is empty — embedding backfill "
            "makes paid Voyage calls and cannot run without a key.",
        ])

    # Pre-flight 2: an active embedding profile must be registered (the
    # tick needs a model_name + dim). Mirrors TS lcm-command.ts:1796-1812.
    try:
        active = get_active_embedding_model(db)
    except sqlite3.Error as exc:
        logger.exception("[worker] active-profile lookup failed")
        return "\n".join([title, f"Skipped: profile lookup failed — {exc!s}."])
    if active is None:
        return "\n".join([
            title,
            "Skipped: no active embedding model registered in "
            "`lcm_embedding_profile` — register one before backfilling.",
        ])

    # Run the tick. The orchestrator's tick_embedding_backfill is async
    # (awaits Voyage HTTP); bridge to it on a fresh event loop. The
    # VoyageClient is created inside _run_backfill_tick so its httpx pool
    # is closed when the loop exits — the tick is one-shot.
    try:
        result = asyncio.run(_run_backfill_tick(db, active.model_name, active.dim, api_key))
    except Exception as exc:  # noqa: BLE001 — operator-facing diagnostic
        # tick_embedding_backfill re-raises only fatal Voyage auth errors;
        # per-batch failures are surfaced in the result. Anything that
        # escapes here is rendered as a one-line failure rather than a
        # crash.
        logger.exception("[worker] embedding-backfill tick failed")
        return "\n".join([title, f"Skipped: backfill tick failed — {exc!s}."])

    # Render the outcome. Three variants — lock-held, empty-queue,
    # processed — mirroring TS buildWorkerTickBackfillText.
    if result.lock_not_acquired:
        # A peer worker held the cross-process lock. Report the holder.
        holder = _describe_lock_holder(db)
        return "\n".join([
            title,
            f"Skipped: embedding-backfill lock held by {holder}",
        ])

    if result.embedded_count == 0 and not result.skipped and result.skipped_over_cap == 0:
        # No work happened and nothing was filtered — the queue was empty.
        return "\n".join([
            title,
            "Skipped: queue is empty (no unembedded leaves)",
        ])

    # Processed path. Compute the estimated cost (Voyage bills per token;
    # the per-token rate is not wired here so we report an order-of-
    # magnitude figure derived from the token count — see _estimate_cost_usd).
    voyage_calls = result.embedded_count
    cost = _estimate_cost_usd(result.voyage_tokens_consumed)
    lines = [
        title,
        f"Processed: {result.embedded_count} embeddings "
        f"(Voyage calls: {voyage_calls}; "
        f"Voyage tokens: {result.voyage_tokens_consumed:,}; "
        f"estimated cost: ${cost:.3f})",
    ]

    # Remaining queue — count what a follow-up tick would still process.
    try:
        from lossless_hermes.embeddings.backfill import count_pending_docs

        remaining = count_pending_docs(db, model_name=active.model_name)
        lines.append(f"Remaining queue: {remaining:,} leaves")
    except sqlite3.Error:
        # Non-fatal — the tick itself succeeded; just omit the count.
        logger.exception("[worker] remaining-queue count failed")

    # Tick latency — duration_ms is measured via time.monotonic by the
    # inner tick (safe under wall-clock skew).
    lines.append(f"Tick latency: {result.duration_ms / 1000.0:.1f} s")

    if result.skipped:
        lines.append(f"Skipped (per-doc failures): {len(result.skipped)}")
    if result.skipped_over_cap > 0:
        lines.append(
            f"Skipped (over-cap, NOT embeddable): {result.skipped_over_cap} — "
            "re-summarize at a smaller cap to bring into range."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Status rendering
# ---------------------------------------------------------------------------


def _render_status(parsed: Any) -> str:
    """Render the read-only ``/lcm worker status`` snapshot.

    Ports the TS ``buildWorkerStatusText`` at
    ``lcm-command.ts:1726-1765``. Pulls a
    :class:`~lossless_hermes.operator.worker_orchestrator.WorkerStatusSnapshot`
    (pure-read-only; no LLM calls, no DB writes) and renders the per-kind
    lock state + pending backlog counts.

    Stale workers (``last_heartbeat_at`` older than the TTL window) are
    flagged ``STALE`` — driven by
    :attr:`~lossless_hermes.operator.worker_orchestrator.WorkerLockSnapshot.is_stale`
    (AC line 92).
    """
    header = _build_header_lines()

    db = _resolve_db(parsed)
    if db is None:
        return "\n".join([
            *header,
            "",
            "### Worker Status",
            "",
            "**Status**",
            "  db: not yet opened",
            "  hint: Send at least one message to trigger on_session_start.",
        ])

    # Resolve the active embedding model so the snapshot can populate the
    # embedding-backfill pending count (it is per-model; the orchestrator
    # does not resolve the active one — see get_worker_status_snapshot).
    model_name: str | None = None
    try:
        active = get_active_embedding_model(db)
        if active is not None:
            model_name = active.model_name
    except sqlite3.Error:
        # Non-fatal — the snapshot still renders; backfill pending shows
        # "(model not registered)".
        logger.exception("[worker] active-profile lookup failed for status")

    try:
        snapshot = get_worker_status_snapshot(db, model_name=model_name)
    except sqlite3.Error as exc:
        logger.exception("[worker] worker status snapshot failed")
        return "\n".join([
            *header,
            "",
            "### Worker Status",
            "",
            f"**Status**\n  query failed: {exc!s}",
        ])

    lines: list[str] = [*header, "", "### Worker Status", ""]
    lines.extend(_format_lock_lines(snapshot))
    lines.append("")
    lines.append("### Pending Work")
    lines.append("")
    lines.extend(_format_pending_lines(snapshot))
    return "\n".join(lines)


def _format_lock_lines(snapshot: WorkerStatusSnapshot) -> list[str]:
    """Render the per-kind lock-state lines.

    One line per :data:`~lossless_hermes.concurrency.model.WORKER_JOB_KINDS`
    literal. Held locks show the worker id + acquired/expires timestamps;
    a stale lock (heartbeat older than TTL) appends a ``STALE`` marker
    (AC line 92). Idle kinds show ``idle (no lock held)``.

    Ports the TS loop at ``lcm-command.ts:1738-1746``, extended with the
    ``STALE`` flag the TS ``buildWorkerStatusText`` did not surface (the
    TS renderer pre-dated the snapshot's ``is_stale`` field; the Python
    port wires it per the issue spec AC line 92).
    """
    out: list[str] = []
    for kind in WORKER_JOB_KINDS:
        lock = snapshot.locks.get(kind)
        if lock is None:
            out.append(f"- **{kind}**: idle (no lock held)")
            continue
        info = lock.lock
        stale_marker = " STALE" if lock.is_stale else ""
        metadata = info.job_metadata if info.job_metadata else "(none)"
        out.append(
            f"- **{kind}**: HELD by `{info.worker_id}`{stale_marker} "
            f"(acquired {info.acquired_at}, expires {info.expires_at}); "
            f"heartbeat={info.last_heartbeat_at}; jobMetadata={metadata}"
        )
    return out


def _format_pending_lines(snapshot: WorkerStatusSnapshot) -> list[str]:
    """Render the pending-backlog lines.

    Ports the TS pending block at ``lcm-command.ts:1748-1753``. The
    ``embedding_backfill`` counter is ``-1`` when no active embedding
    model is registered (the count is per-model — see
    :class:`~lossless_hermes.operator.worker_orchestrator.PendingCounts`);
    that sentinel renders as ``(model not registered)``.
    """
    pending = snapshot.pending
    embedding = (
        "(model not registered)"
        if pending.embedding_backfill < 0
        else f"{pending.embedding_backfill:,}"
    )
    extraction = (
        "(not queryable)" if pending.extraction_queue < 0 else f"{pending.extraction_queue:,}"
    )
    return [
        f"- Embedding backfill pending: {embedding}",
        f"- Extraction queue: {extraction}",
    ]


# ---------------------------------------------------------------------------
# Tick helpers
# ---------------------------------------------------------------------------


async def _run_backfill_tick(
    db: sqlite3.Connection,
    model_name: str,
    dim: int,
    api_key: str,
) -> Any:
    """Async body of the backfill tick — owns the :class:`VoyageClient` lifecycle.

    Instantiates a one-shot :class:`~lossless_hermes.voyage.client.VoyageClient`,
    runs the orchestrator's
    :func:`~lossless_hermes.operator.worker_orchestrator.tick_embedding_backfill`,
    and closes the client's httpx pool in ``finally`` (the tick is
    one-shot — no cross-tick client reuse, unlike the worker loop).

    The ``voyage_output_dimension=dim`` argument is load-bearing: the
    LCM Wave-12 reviewer P1 fix passes the profile's ``dim`` so
    non-default (256/512/2048) profiles get the right-shape vectors —
    without it Voyage returns 1024-dim vectors and ``record_embedding``
    rejects them as length-mismatched.
    """
    from lossless_hermes.voyage.client import VoyageClient

    voyage = VoyageClient(api_key=api_key)
    try:
        return await tick_embedding_backfill(
            db,
            model_name=model_name,
            voyage_model=model_name,
            voyage=voyage,
            input_type="document",
            # LCM Wave-12 reviewer P1: pass profile.dim so non-default
            # profiles get right-shape vectors.
            voyage_output_dimension=dim,
        )
    finally:
        await voyage.aclose()


def _describe_lock_holder(db: sqlite3.Connection) -> str:
    """Describe the current holder of the ``embedding-backfill`` lock.

    Best-effort — used only to enrich the "lock held by peer" skip
    message. If the lock row vanished between the tick's acquire-fail and
    this lookup (a peer released it), returns a generic ``"a peer
    worker"`` rather than raising.
    """
    try:
        from lossless_hermes.concurrency.worker_lock import lock_info

        info = lock_info(db, "embedding-backfill")
    except sqlite3.Error:
        logger.exception("[worker] lock-holder lookup failed")
        return "a peer worker"
    if info is None:
        return "a peer worker"
    return f"host={info.worker_id} since {info.acquired_at}"


def _estimate_cost_usd(voyage_tokens: int) -> float:
    """Estimate the USD cost of ``voyage_tokens`` Voyage embedding tokens.

    Voyage bills per million input tokens. The exact per-model rate is
    not wired into this repo (it is operator-config / billing-tier
    dependent), so this uses a representative reference rate of
    ``$0.12 / 1M tokens`` (voyage-3-class pricing as of the LCM
    ``1f07fbd`` snapshot) purely to give the operator an order-of-
    magnitude figure in the tick output. The result is informational —
    the authoritative spend is the ``Voyage tokens`` count, which is
    reported alongside it.
    """
    _RATE_USD_PER_MILLION: Final[float] = 0.12
    return (voyage_tokens / 1_000_000.0) * _RATE_USD_PER_MILLION


def _build_tick_error(message: str) -> str:
    """Render a ``[lcm] worker tick`` error block.

    The AC (line 94) mandates the exact prefix
    ``[lcm] worker tick: unknown kind '...'. Valid kinds: ...`` for the
    unknown-kind case; this helper produces ``[lcm] worker tick: <message>``
    so all worker-tick dispatch errors share the prefix.
    """
    return f"[lcm] worker tick: {message}"


# ---------------------------------------------------------------------------
# Engine / DB resolution
# ---------------------------------------------------------------------------


def _resolve_db(parsed: Any) -> sqlite3.Connection | None:
    """Resolve the SQLite connection from ``parsed.engine``.

    The engine exposes its connection via different attributes depending
    on the engine state (Epic 02 noop engine vs Epic 03 wired engine).
    We probe both shapes — first the canonical ``_db`` attribute used by
    the wired engine, then the alternatives used by test fixtures and
    older engine builds. Returns ``None`` on miss (the handler renders a
    friendly "DB unavailable" message instead of an AttributeError stack
    trace).

    Mirrors the ``_resolve_db`` helper in
    :mod:`lossless_hermes.commands.purge` /
    :mod:`lossless_hermes.commands.reconcile` for cross-command
    consistency.
    """
    engine = getattr(parsed, "engine", None)
    if engine is None:
        return None
    for attr in ("_db", "db_connection", "db", "_conn", "conn"):
        candidate = getattr(engine, attr, None)
        if isinstance(candidate, sqlite3.Connection):
            return candidate
    return None
