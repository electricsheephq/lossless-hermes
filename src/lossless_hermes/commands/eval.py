"""``/lcm eval`` — recall eval against fts / semantic / hybrid retrieval (Epic 08-13).

Replaces the Epic 08-01 router stub with the real handler. ``/lcm eval``
runs a recall@K eval of a registered query set against the active
retrieval surface and reports recall + drift to the operator.

Wires the 08-13 eval runner (:func:`lossless_hermes.operator.eval_runner.run_eval`)
into the slash command: the runner is provider-agnostic — it takes an
INJECTED retrieval adapter — so this handler picks the adapter for the
requested ``--mode`` and bridges to the (async) runner.

Ports the TS ``case "eval"`` parser at
``lossless-claw/src/plugin/lcm-command.ts:446-472`` plus the
``buildEvalText`` renderer + ``buildFtsOnlyAdapter`` /
``buildHybridAdapter`` adapter builders at lines 1965-2118.

### CLI surface (per ``epics/08-cli-ops/08-13-eval-runner.md`` line 26)

::

    /lcm eval [--baseline] [--mode <fts_only|semantic_only|hybrid>]
              [--query-set <name>] [--version <int>]

* Required: ``--baseline`` OR ``--mode``. A bare ``/lcm eval`` is
  ambiguous and rejected (TS parity, ``lcm-command.ts:457-463``).
* ``--baseline`` is a shorthand: it selects ``fts_only`` mode against
  the default ``eva-baseline v1`` query set (TS parity,
  ``lcm-command.ts:455-456``).
* ``--query-set`` defaults to :data:`_DEFAULT_QUERY_SET_NAME`;
  ``--version`` defaults to :data:`_DEFAULT_QUERY_SET_VERSION`.

### Modes + adapters

* ``fts_only`` — :func:`_build_fts_only_adapter` wraps
  :meth:`SummaryStore.search_summaries` in ``mode='full_text'``. No
  embedding, no Voyage cost.
* ``hybrid`` — :func:`_build_hybrid_adapter` wraps
  :func:`~lossless_hermes.embeddings.hybrid_search.run_hybrid_search`
  (RRF fusion, ``rerank=False``). **Paid Voyage cost** — embeds the
  query. Degrades to FTS-only per-query if vec0 / Voyage is
  unavailable.
* ``semantic_only`` — wired through the hybrid adapter for the v4.1
  first cut (TS parity, ``lcm-command.ts:2071-2082``: ``semantic_only``
  is not separately wired; the hybrid adapter already covers the
  "vec0 absent → fall back to FTS" case so the operator still gets a
  meaningful result).

### Async bridge

The dispatcher's :meth:`LcmCommandDispatcher.handle` is sync, but
:func:`~lossless_hermes.operator.eval_runner.run_eval` is ``async``
(the recall loop awaits the adapter). We bridge via :func:`asyncio.run`
on a fresh event loop — the same pattern :mod:`lossless_hermes.commands.worker`
uses for the embedding-backfill tick (ADR-017: sync-by-design
command surface).

### Owner-gating per ADR-013

Owner-gating is **upstream** — Hermes's ``SlashAccessPolicy`` gates the
``allow_admin_from`` config BEFORE this handler runs (the dispatcher
table marks ``eval`` ``owner_gated=True``). The handler does NOT
re-check owner status. ``/lcm eval`` is owner-gated because it writes
``lcm_eval_run`` rows and may cost Voyage tokens in hybrid mode
(TS parity, ``lcm-command.ts:2784-2807``).

See:

* ``epics/08-cli-ops/08-13-eval-runner.md`` — the eval-runner issue.
* ``src/lossless_hermes/operator/eval_runner.py`` — the runner this
  handler bridges to.
* ``docs/adr/013-owner-gating.md`` — caller-side gating.
* ``docs/adr/017-sync-db-surface.md`` — sync-by-design surface; the
  ``asyncio.run`` bridge.
* ``lossless-claw/src/plugin/lcm-command.ts:282-336, 446-472,
  1965-2118, 2784-2816`` — TS source pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from lossless_hermes.eval.query_set import QuerySetIdentity, encode_query_set_id
from lossless_hermes.eval.recall import RecallSearchAdapter
from lossless_hermes.operator.eval_runner import (
    EvalMode,
    EvalRunnerError,
    RunEvalArgs,
    format_eval_report,
    run_eval,
)

logger = logging.getLogger("lossless_hermes.commands.eval")


# ---------------------------------------------------------------------------
# Constants — port from ``lcm-command.ts:289-291``
# ---------------------------------------------------------------------------

_EVAL_MODES: Final[tuple[EvalMode, ...]] = ("fts_only", "semantic_only", "hybrid")
"""Valid ``--mode`` values. Ports TS ``EVAL_MODES`` (``lcm-command.ts:289``)."""

_DEFAULT_QUERY_SET_NAME: Final[str] = "eva-baseline"
"""Default ``--query-set`` when omitted. Ports TS
``DEFAULT_EVAL_QUERY_SET_NAME`` (``lcm-command.ts:290``)."""

_DEFAULT_QUERY_SET_VERSION: Final[int] = 1
"""Default ``--version`` when omitted. Ports TS
``DEFAULT_EVAL_QUERY_SET_VERSION`` (``lcm-command.ts:291``)."""

_FTS_SEARCH_LIMIT: Final[int] = 50
"""Per-query FTS candidate count. Ports the ``limit: 50`` literal in TS
``buildFtsOnlyAdapter`` (``lcm-command.ts:1978``)."""

_TITLE: Final[str] = "[lcm] eval"
"""Operator-facing title line prefix. The Python port renders plain
text (the Hermes ``register_command`` contract returns ``str``); the TS
``🧪 Lossless Claw Eval`` banner is collapsed to this ASCII title for
parity with the sibling commands' ``[lcm] <sub>`` titles."""


# ---------------------------------------------------------------------------
# Parsed-args dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EvalArgs:
    """Internal result of :func:`_parse_eval_args`.

    Ports the TS ``EvalParseResult`` interface (``lcm-command.ts:282-288``)
    PLUS the resolved/defaulted fields the TS ``case "eval"`` block
    computes after parsing (``lcm-command.ts:455-465``).

    Attributes:
        mode: The resolved retrieval mode — never ``None`` on a
            successful parse (``--baseline`` resolves it to
            ``fts_only``).
        query_set_name: Resolved query-set name (default applied).
        query_set_version: Resolved query-set version (default applied).
        parse_error: Operator-facing message when the parse failed.
            When set, all other fields are placeholder values and the
            handler renders the error block instead of running.
    """

    mode: EvalMode = "fts_only"
    query_set_name: str = _DEFAULT_QUERY_SET_NAME
    query_set_version: int = _DEFAULT_QUERY_SET_VERSION
    parse_error: str | None = None


# ---------------------------------------------------------------------------
# Public handler — invoked by LcmCommandDispatcher
# ---------------------------------------------------------------------------


def run(parsed: Any) -> str:
    """``/lcm eval`` handler — run a recall eval + report recall/drift.

    **OWNER-GATED** (upstream, per ADR-013 — this handler does NOT
    re-check). Writes an ``lcm_eval_run`` row; ``hybrid`` /
    ``semantic_only`` modes embed the query (paid Voyage cost).

    Steps:

    1. Parse ``--baseline`` / ``--mode`` / ``--query-set`` / ``--version``
       from the raw args (see :func:`_parse_eval_args`). A parse error
       — including the "bare ``/lcm eval``" ambiguity — renders an
       error block and returns.
    2. Resolve the engine DB connection. No DB → friendly
       "unavailable" block (no stack trace).
    3. Build the retrieval adapter for the requested mode
       (:func:`_build_adapter_for_mode`).
    4. Bridge to the async :func:`~lossless_hermes.operator.eval_runner.run_eval`
       via :func:`asyncio.run`.
    5. Render the recall + drift report via
       :func:`~lossless_hermes.operator.eval_runner.format_eval_report`,
       or — on :class:`EvalRunnerError` (missing / empty query set) —
       an error block naming the failure ``kind``.

    Args:
        parsed: :class:`~lossless_hermes.plugin.commands.ParsedLcmCommand`.
            ``parsed.raw_args`` is re-tokenized here (the router's
            pre-parse only extracts the bare ``--baseline`` flag, not
            ``--mode`` / ``--query-set`` / ``--version`` — see
            :func:`_parse_eval_args`). ``parsed.engine`` carries the
            :class:`LCMEngine`.

    Returns:
        A multi-line operator-facing text block. Always non-empty;
        never raises out (parse error / DB-unavailable / runner error
        all render as a section, per the dispatcher's "be robust"
        contract).
    """
    args = _parse_eval_args(parsed)

    if args.parse_error:
        return _build_text(
            sections=[
                (
                    "Eval",
                    [
                        ("status", "rejected"),
                        ("kind", "parse_error"),
                        ("reason", args.parse_error),
                    ],
                ),
            ],
        )

    plan_section = (
        "Plan",
        [
            ("query set", f"{args.query_set_name} v{args.query_set_version}"),
            ("mode", f"`{args.mode}`"),
        ],
    )

    db = _resolve_db(parsed)
    if db is None:
        return _build_text(
            sections=[
                plan_section,
                (
                    "Eval",
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

    identity = QuerySetIdentity(name=args.query_set_name, version=args.query_set_version)

    # Notes section — mode-specific operator hints (vec0 / semantic_only),
    # mirroring the TS warning/note sections (lcm-command.ts:2062-2082).
    notes = _mode_notes(db, args.mode)

    adapter = _build_adapter_for_mode(db, args.mode)

    # The runner is async (the recall loop awaits the adapter). Bridge to
    # it on a fresh event loop — same pattern as commands/worker.py per
    # ADR-017 (sync-by-design command surface).
    try:
        result = asyncio.run(
            run_eval(
                db,
                RunEvalArgs(
                    query_set_identity=identity,
                    mode=args.mode,
                    retrieval_adapter=adapter,
                ),
            )
        )
    except EvalRunnerError as exc:
        # Missing / empty query set — render the kind so the operator
        # can distinguish "set not registered" from "set empty".
        # Mirrors TS buildEvalText's EvalRunnerError branch
        # (lcm-command.ts:2100-2108).
        return _build_text(
            sections=[
                plan_section,
                *notes,
                (
                    "Eval",
                    [
                        ("status", "failed"),
                        ("kind", exc.kind),
                        ("reason", str(exc)),
                    ],
                ),
            ],
        )
    except Exception as exc:  # noqa: BLE001 — operator-facing diagnostic
        # Adapter failure (vec0 deadlock, Voyage auth in a non-degrading
        # path, SQLite error) — surface as a one-line failure rather
        # than a crash. Mirrors TS buildEvalText's generic catch
        # (lcm-command.ts:2110-2115).
        logger.exception("[eval] run failed for %s", encode_query_set_id(identity))
        return _build_text(
            sections=[
                plan_section,
                *notes,
                (
                    "Eval",
                    [
                        ("status", "failed"),
                        ("reason", str(exc)),
                    ],
                ),
            ],
        )

    # Happy path — render the recall + drift report.
    report = format_eval_report(identity, args.mode, result)
    return _build_text(
        sections=[
            plan_section,
            *notes,
            ("Result", [report]),
        ],
    )


# ---------------------------------------------------------------------------
# Parser — re-tokenize parsed.raw_args into _EvalArgs
# ---------------------------------------------------------------------------


def _parse_eval_args(parsed: Any) -> _EvalArgs:
    """Parse the parsed-command into an :class:`_EvalArgs`.

    Ports the TS ``parseEvalArgs`` parser (``lcm-command.ts:293-336``)
    plus the resolve/default step the ``case "eval"`` block runs after
    parsing (``lcm-command.ts:455-465``).

    The router's :func:`~lossless_hermes.plugin.commands._preparse_flags`
    only extracts the bare ``--baseline`` flag into ``parsed.flags`` —
    it does NOT handle ``--mode`` / ``--query-set`` / ``--version``
    (valued flags the router doesn't know about). So, like the TS
    ``case "eval"`` block (which re-tokenizes ``rawArgs`` via
    ``splitArgsQuoted``), we re-tokenize ``parsed.raw_args`` here and do
    the full per-subcommand parse ourselves.

    Validation parity with the TS parser:

    * ``--mode`` with no value → error (``lcm-command.ts:305``).
    * ``--mode`` with an unknown value → error naming valid modes
      (``lcm-command.ts:306-308``).
    * ``--query-set`` with no value → error (``lcm-command.ts:315``).
    * ``--version`` with no value → error (``lcm-command.ts:321``).
    * ``--version`` non-positive-integer → error
      (``lcm-command.ts:323-326``).
    * Unknown flag / bare positional → error (``lcm-command.ts:331``).
    * Neither ``--baseline`` nor ``--mode`` → "ambiguous" error
      (``lcm-command.ts:457-463``).

    Args:
        parsed: :class:`ParsedLcmCommand` with ``raw_args`` (the full
            string after ``/lcm``, starting with ``eval``).

    Returns:
        :class:`_EvalArgs` — on parse error, ``parse_error`` is set to
        the operator-facing message and the other fields are
        placeholders.
    """
    raw = getattr(parsed, "raw_args", "") or ""

    # Re-tokenize. The raw args start with the ``eval`` subcommand token;
    # strip it (case-insensitively) so we parse only the flags. Mirrors
    # the TS ``rawArgs.slice(idx + "eval".length)`` step
    # (lcm-command.ts:447-449).
    try:
        all_tokens = shlex.split(raw)
    except ValueError as exc:
        return _EvalArgs(parse_error=f"argument parse error — {exc!s}")
    tokens = all_tokens[1:] if all_tokens and all_tokens[0].lower() == "eval" else all_tokens

    baseline = False
    mode: EvalMode | None = None
    query_set_name: str | None = None
    query_set_version: int | None = None

    i = 0
    while i < len(tokens):
        token = tokens[i]
        lowered = token.lower()
        if lowered == "--baseline":
            baseline = True
        elif lowered == "--mode":
            value = tokens[i + 1] if i + 1 < len(tokens) else None
            if not value:
                return _EvalArgs(
                    parse_error="`--mode` requires a value (fts_only|semantic_only|hybrid)."
                )
            if value not in _EVAL_MODES:
                return _EvalArgs(
                    parse_error=(f"Unknown mode `{value}`. Supported: {', '.join(_EVAL_MODES)}.")
                )
            mode = value  # type: ignore[assignment]  — membership-checked above
            i += 1
        elif lowered == "--query-set":
            value = tokens[i + 1] if i + 1 < len(tokens) else None
            if not value:
                return _EvalArgs(parse_error="`--query-set` requires a value.")
            query_set_name = value
            i += 1
        elif lowered == "--version":
            value = tokens[i + 1] if i + 1 < len(tokens) else None
            if not value:
                return _EvalArgs(parse_error="`--version` requires a value.")
            try:
                parsed_version = int(value)
            except ValueError:
                return _EvalArgs(
                    parse_error=f"`--version` must be a positive integer (got `{value}`)."
                )
            if parsed_version < 1:
                return _EvalArgs(
                    parse_error=f"`--version` must be a positive integer (got `{value}`)."
                )
            query_set_version = parsed_version
            i += 1
        else:
            return _EvalArgs(parse_error=f"Unknown argument `{token}` for `/lcm eval`.")
        i += 1

    # Resolve step — port of lcm-command.ts:455-465.
    # Bare `/lcm eval` (neither flag) is ambiguous.
    if mode is None and not baseline:
        return _EvalArgs(
            parse_error=(
                "`/lcm eval` requires `--baseline` or `--mode <fts_only|semantic_only|hybrid>`."
            )
        )
    # --baseline shorthand: defaults to fts_only (TS lcm-command.ts:456).
    resolved_mode: EvalMode = mode if mode is not None else "fts_only"
    return _EvalArgs(
        mode=resolved_mode,
        query_set_name=(query_set_name if query_set_name is not None else _DEFAULT_QUERY_SET_NAME),
        query_set_version=(
            query_set_version if query_set_version is not None else _DEFAULT_QUERY_SET_VERSION
        ),
    )


# ---------------------------------------------------------------------------
# Retrieval adapters — port from ``lcm-command.ts:1970-2037``
# ---------------------------------------------------------------------------


def _build_adapter_for_mode(db: sqlite3.Connection, mode: EvalMode) -> RecallSearchAdapter:
    """Pick the retrieval adapter for ``mode``.

    Ports the adapter-selection block of TS ``buildEvalText``
    (``lcm-command.ts:2057-2082``):

    * ``fts_only`` → :func:`_build_fts_only_adapter`.
    * ``hybrid`` → :func:`_build_hybrid_adapter`.
    * ``semantic_only`` → :func:`_build_hybrid_adapter` (TS first-cut
      parity: ``semantic_only`` is wired through the hybrid adapter,
      ``lcm-command.ts:2071-2082``).
    """
    if mode == "fts_only":
        return _build_fts_only_adapter(db)
    # hybrid + semantic_only both route through the hybrid adapter.
    return _build_hybrid_adapter(db)


def _build_fts_only_adapter(db: sqlite3.Connection) -> RecallSearchAdapter:
    """Build an FTS-only recall adapter.

    Ports TS ``buildFtsOnlyAdapter`` (``lcm-command.ts:1970-1983``).
    Wraps :meth:`~lossless_hermes.store.summary.SummaryStore.search_summaries`
    in ``mode='full_text'`` and collapses the result rows to
    ``summary_id`` strings in rank order. No embedding, no Voyage cost.
    """
    from lossless_hermes.db.features import get_lcm_db_features
    from lossless_hermes.store.summary import SummaryStore, SummarySearchInput

    features = get_lcm_db_features(db)
    store = SummaryStore(
        db,
        fts5_available=features.fts5_available,
        trigram_tokenizer_available=features.fts5_trigram_available,
    )

    class _FtsOnlyAdapter:
        """Recall adapter wrapping ``SummaryStore.search_summaries``."""

        async def search(self, query: Any) -> list[str]:
            hits = store.search_summaries(
                SummarySearchInput(
                    query=query.query_text,
                    mode="full_text",
                    limit=_FTS_SEARCH_LIMIT,
                )
            )
            return [h.summary_id for h in hits]

    return _FtsOnlyAdapter()


def _build_hybrid_adapter(db: sqlite3.Connection) -> RecallSearchAdapter:
    """Build a hybrid (RRF) recall adapter with FTS-only graceful degrade.

    Ports TS ``buildHybridAdapter`` (``lcm-command.ts:1996-2037``).
    Wraps :func:`~lossless_hermes.embeddings.hybrid_search.run_hybrid_search`
    (RRF fusion, ``rerank=False`` — no paid Voyage rerank in the eval
    path). The semantic arm still embeds the query (paid Voyage cost).

    If the hybrid arm fails for a given query (vec0 missing, no Voyage
    key, transient error) the adapter falls back to the FTS-only result
    for that query so the eval still produces a meaningful number —
    mirroring the TS ``catch`` at ``lcm-command.ts:2029-2034``.
    """
    from lossless_hermes.db.features import get_lcm_db_features
    from lossless_hermes.embeddings.hybrid_search import FtsHit, run_hybrid_search
    from lossless_hermes.store.summary import SummaryStore, SummarySearchInput

    features = get_lcm_db_features(db)
    store = SummaryStore(
        db,
        fts5_available=features.fts5_available,
        trigram_tokenizer_available=features.fts5_trigram_available,
    )
    fts_adapter = _build_fts_only_adapter(db)

    async def _fts_search(
        query: str, *, limit: int = _FTS_SEARCH_LIMIT, **_kwargs: Any
    ) -> list[FtsHit]:
        """FTS arm for :func:`run_hybrid_search`.

        ``query`` is **positional-or-keyword** — :data:`FtsSearchFn`'s
        contract is ``async def fts_search(query: str, *, limit, **filters)``
        and ``run_hybrid_search`` invokes the injected function with
        ``query`` POSITIONAL (``hybrid_search.py``:
        ``fts_search(query_stripped, limit=k_fts, **kwargs)``). A
        keyword-only ``query`` here would raise ``TypeError`` — swallowed
        by :meth:`_HybridAdapter.search`'s ``except`` — so hybrid /
        semantic_only would silently degrade to FTS-only on every query.

        ``run_hybrid_search`` forwards filter kwargs (``session_keys``,
        ``conversation_ids``, ...) which the eval path does not use —
        absorbed via ``**_kwargs``. Returns :class:`FtsHit` rows; the
        eval path only consumes ``summary_id`` but the hybrid pipeline
        needs the full shape for RRF.
        """
        rows = store.search_summaries(
            SummarySearchInput(query=query, mode="full_text", limit=limit)
        )
        return [
            FtsHit(
                summary_id=row.summary_id,
                conversation_id=row.conversation_id,
                session_key="",
                kind=row.kind,
                content=row.snippet,
                token_count=0,
                created_at=row.created_at.isoformat(),
                rank=idx,
            )
            for idx, row in enumerate(rows)
        ]

    class _HybridAdapter:
        """Recall adapter wrapping ``run_hybrid_search`` (RRF, no rerank)."""

        async def search(self, query: Any) -> list[str]:
            try:
                result = await run_hybrid_search(
                    db,
                    query=query.query_text,
                    fts_search=_fts_search,
                    # RRF fusion only — no Voyage rerank cost in the eval
                    # path (TS lcm-command.ts:2009).
                    rerank=False,
                )
                return [h.summary_id for h in result.hits]
            except Exception as exc:  # noqa: BLE001 — per-query graceful degrade
                # Hybrid arm unavailable (vec0 missing, no key, etc.) —
                # fall back to FTS-only for this single query so the
                # eval still produces a result. Mirrors TS
                # lcm-command.ts:2029-2034.
                #
                # Log the exception itself (``%r`` of ``exc``), not just a
                # generic message: a programming error such as a signature
                # mismatch in ``_fts_search`` would otherwise be silently
                # masked as a routine vec0/Voyage degrade.
                logger.warning(
                    "[eval] hybrid arm failed for query %r; degrading to FTS-only: %r",
                    getattr(query, "query_id", "<unknown>"),
                    exc,
                )
                return await fts_adapter.search(query)

    return _HybridAdapter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mode_notes(
    db: sqlite3.Connection, mode: EvalMode
) -> list[tuple[str, Sequence[tuple[str, str] | str]]]:
    """Build mode-specific operator note sections.

    Ports the warning / note sections of TS ``buildEvalText``
    (``lcm-command.ts:2062-2082``):

    * ``hybrid`` + vec0 absent → a "Warning" section: hybrid will
      degrade to FTS-only inside the adapter.
    * ``semantic_only`` → a "Note" section: ``semantic_only`` is wired
      through the hybrid adapter for the v4.1 first cut.

    Returns an empty list when no note applies (``fts_only``, or
    ``hybrid`` with vec0 present).

    The return element type matches :func:`_build_text`'s ``sections``
    element type (``tuple[str, Sequence[tuple[str, str] | str]]``) so
    the note sections splice cleanly into the call sites' section lists
    alongside the ``(key, value)``-only sections and the plain-string
    ``Result`` section.
    """
    from lossless_hermes.db.features import get_lcm_db_features

    notes: list[tuple[str, Sequence[tuple[str, str] | str]]] = []
    if mode == "semantic_only":
        notes.append((
            "Note",
            [
                (
                    "semantic_only",
                    "wired through the hybrid adapter for the v4.1 first cut "
                    "(RRF fusion, no rerank).",
                ),
            ],
        ))
    if mode in ("hybrid", "semantic_only"):
        try:
            vec0_available = get_lcm_db_features(db).vec0_available
        except sqlite3.Error:
            vec0_available = False
        if not vec0_available:
            notes.append((
                "Warning",
                [
                    (
                        "vec0",
                        "not loaded — hybrid mode will degrade to FTS-only inside the adapter.",
                    ),
                ],
            ))
    return notes


def _build_text(
    sections: Sequence[tuple[str, Sequence[tuple[str, str] | str]]],
) -> str:
    """Render the multi-section operator text block.

    Format mirrors the sibling-command renderers (``commands/reconcile.py``
    ``_build_text``) and collapses the TS ``buildEvalText`` markdown
    banner to a plain-text title:

    ::

        [lcm] eval

        Section Name:
          key: value
          key: value
        ...

    Each section is a ``(heading, items)`` tuple. ``items`` may be a
    list of ``(key, value)`` tuples (rendered as ``  key: value``) or a
    list of plain strings (rendered as ``  string`` — used for the
    multi-line ``format_eval_report`` block).

    Args:
        sections: ``[(heading, items), ...]``. ``items`` is either
            ``[(key, value), ...]`` or ``[str, ...]``.

    Returns:
        Single string with all sections joined by newlines.
    """
    lines: list[str] = [_TITLE, ""]
    for heading, items in sections:
        lines.append(f"{heading}:")
        for item in items:
            if isinstance(item, tuple):
                key, value = item
                lines.append(f"  {key}: {value}")
            else:
                # Plain-string item — may itself be multi-line (the
                # format_eval_report block). Indent each physical line.
                for physical_line in item.split("\n"):
                    lines.append(f"  {physical_line}")
        lines.append("")
    # Trim the trailing blank line for compactness.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


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
    :mod:`lossless_hermes.commands.worker` /
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
