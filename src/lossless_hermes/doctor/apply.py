"""Doctor apply-mode — per-conversation broken-summary repair.

Ports ``lossless-claw/src/plugin/lcm-doctor-apply.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``) — the per-conversation
summary-repair path of ``/lcm doctor apply``.

:func:`apply_scoped_doctor_repair` re-summarizes broken summaries (rows
that :func:`lossless_hermes.doctor.shared.detect_doctor_marker` flags as
fallback/truncated) by re-running the active summarizer, then writes the
rewrites in a single ``BEGIN IMMEDIATE`` transaction. Mutates
``summaries.content``, ``summaries.token_count``, and the
``summaries_fts`` mirror (best-effort). Returns a :class:`DoctorApplyResult`
describing what was detected, repaired, left unchanged, and skipped.

### Repair order (load-bearing)

Targets are repaired **leaves-first, then condensed**:

1. **Active leaves** — ordered by ``context_items.ordinal`` ASC.
2. **Orphan leaves** (a leaf with a marker but no ``context_items``
   row) — ordered by ``(depth, created_at, summary_id)``.
3. **Condensed summaries** — ordered by ``(depth, created_at,
   summary_id)``.

The leaves-first ordering is *required for correctness*: condensed
re-summarization reads its leaf children's content from the in-memory
``overrides`` map (see :func:`_build_condensed_source_text`). If a
condensed summary were repaired before its leaf children, it would
re-summarize stale (still-broken) leaf content.

### Summarizer seam (the LLM coupling)

Per ``docs/porting-guides/doctor-ops.md`` §"Remaining 5% risk" #2, the
TS module pulls in ``createLcmSummarizeFromLegacyParams`` (a
plugin-specific summarizer factory) plus ``LcmDependencies`` (a DI
shape). The Python port consumes Epic 04's
:class:`lossless_hermes.summarize.LcmSummarizer` — the SAME class used by
leaf/condensed compaction — so provider resolution, the fallback chain,
and auth-failure detection are reused without duplication.

Two ways to supply the summarizer (mirroring the TS ``summarize?`` /
``deps?`` params):

* ``summarize`` — an explicit callable matching the TS ``LcmSummarizeFn``
  shape: ``(text, aggressive, LcmSummarizeOptions) -> str``. This is the
  test seam and the highest-priority resolution layer.
* ``deps`` — a :class:`lossless_hermes.summarize.SummarizerDeps`
  implementation. When ``summarize`` is not given, a
  :class:`~lossless_hermes.summarize.LcmSummarizer` is constructed from
  ``deps`` + ``config`` and its bound ``summarize`` method is used. If
  construction raises (no provider configured, the chain resolved
  empty, etc.) the function returns ``{"kind": "unavailable"}`` — it
  never raises out.

Doctor-apply's prompt construction differs from compaction's in one
respect: it forwards a **"repair context"** stanza (the previous-summary
text resolved by :func:`_resolve_previous_summary_context`) as the
:attr:`~lossless_hermes.summarize.LcmSummarizeOptions.previous_summary`
option so the LLM has surrounding context for accurate re-summarization.
The leaf vs condensed prompts otherwise reuse Epic 04-05's verbatim
templates (inside :class:`~lossless_hermes.summarize.LcmSummarizer`).

See:

* ``epics/08-cli-ops/08-07-doctor-apply.md`` — this issue spec.
* ``docs/porting-guides/doctor-ops.md`` §"Doctor marker detection"
  lines 202-212 — the canonical algorithm.
* ``docs/adr/017-db-surface-sync.md`` — why this module is sync-only.
* ``lossless-claw/src/plugin/lcm-doctor-apply.ts:1-541`` — TS source
  pinned at commit ``1f07fbd`` on branch ``pr-613``.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from lossless_hermes.compaction import _format_timestamp
from lossless_hermes.doctor.contract import DoctorApplyResult, DoctorTargetRecord
from lossless_hermes.doctor.shared import detect_doctor_marker, load_doctor_targets
from lossless_hermes.estimate_tokens import estimate_tokens
from lossless_hermes.summarize import LcmSummarizer, LcmSummarizeOptions, SummarizerDeps
from lossless_hermes.transaction_mutex import with_database_transaction

__all__ = [
    "DoctorSummarizeFn",
    "apply_scoped_doctor_repair",
]

logger = logging.getLogger("lossless_hermes.doctor.apply")


# ---------------------------------------------------------------------------
# Summarizer seam type
# ---------------------------------------------------------------------------

#: The summarizer callable doctor-apply consumes. Ports the TS
#: ``LcmSummarizeFn`` (``summarize.ts:11-15``):
#:
#: .. code-block:: typescript
#:
#:     export type LcmSummarizeFn = (
#:       text: string, aggressive?: boolean, options?: LcmSummarizeOptions,
#:     ) => Promise<string>;
#:
#: The Python form is **sync** (per ADR-017 — Hermes's
#: ``auxiliary_client.call_llm`` is synchronous, so the whole doctor
#: pipeline stays sync end-to-end) and takes a typed
#: :class:`~lossless_hermes.summarize.LcmSummarizeOptions` rather than a
#: loose object. :meth:`lossless_hermes.summarize.LcmSummarizer.summarize`
#: already satisfies this shape, so the resolved-from-``deps`` path needs
#: no adapter.
DoctorSummarizeFn = Callable[[str, bool, LcmSummarizeOptions], str]


# ---------------------------------------------------------------------------
# Internal value types (port of the TS module-local type aliases)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SummaryOverride:
    """An in-memory rewrite of one summary, pending the final write.

    Ports the TS ``SummaryOverride`` type (``lcm-doctor-apply.ts:11-14``).
    Carries the new ``content`` and its re-estimated ``token_count``.
    Lives in the per-pass ``overrides`` map so condensed re-summarization
    can read the freshly-rewritten content of its leaf children.
    """

    content: str
    token_count: int


@dataclass(frozen=True)
class _SummaryTimeRange:
    """Resolved ``[earliest, latest]`` window for a condensed child header.

    Ports the TS ``SummaryTimeRange`` type (``lcm-doctor-apply.ts:16-19``).
    Either bound may be :data:`None` when the source row has no usable
    timestamp; :func:`_format_summary_time_range` then emits an empty
    header.
    """

    earliest_at: Optional[datetime]
    latest_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def apply_scoped_doctor_repair(
    *,
    db: sqlite3.Connection,
    config: Any,
    conversation_id: int,
    deps: Optional[SummarizerDeps] = None,
    summarize: Optional[DoctorSummarizeFn] = None,
    runtime_config: Any = None,
) -> DoctorApplyResult:
    """Repair broken summaries for a single resolved conversation.

    Verbatim-behavior port of TS ``applyScopedDoctorRepair``
    (``lcm-doctor-apply.ts:49-170``). The TS function is ``async``; this
    port is sync per ADR-017 (the summarizer seam is sync in Hermes).

    Algorithm (see module docstring + ``doctor-ops.md`` lines 202-212):

    1. Load doctor targets for ``conversation_id``. No targets → return
       an empty ``"applied"`` result immediately (no summarizer needed).
    2. Resolve the summarizer (``summarize`` arg → built from ``deps``).
       Unresolvable → return ``{"kind": "unavailable", "reason": ...}``.
    3. Order targets leaves-first (see :func:`_order_doctor_targets`).
    4. For each target: build source text, resolve previous-summary
       context, call the summarizer, validate the output. Successful
       rewrites land in the in-memory ``overrides`` map; failures land
       in ``skipped``.
    5. Write all rewrites in ONE ``BEGIN IMMEDIATE`` transaction at the
       end — so a partial mid-loop failure rolls back every write.

    Args:
        db: Open :class:`sqlite3.Connection` for the LCM database.
        config: :class:`~lossless_hermes.db.config.LcmConfig` instance
            (or a mapping with the same field names — used in tests).
            Read for ``timezone`` and ``custom_instructions``.
        conversation_id: The conversation whose broken summaries are
            repaired. Doctor-apply is always scoped to one conversation
            (the TS plugin resolves it from request context; the Hermes
            CLI passes it explicitly).
        deps: Optional :class:`~lossless_hermes.summarize.SummarizerDeps`.
            Used to construct an
            :class:`~lossless_hermes.summarize.LcmSummarizer` when
            ``summarize`` is not supplied.
        summarize: Optional explicit summarizer callable
            (:data:`DoctorSummarizeFn`). Highest-priority resolution
            layer; the test seam.
        runtime_config: Accepted for TS signature parity
            (``runtimeConfig`` in TS) and forwarded to the summarizer
            factory as host-runtime config. Unused when ``summarize`` is
            supplied directly.

    Returns:
        :class:`DoctorApplyResult`. ``kind="applied"`` when the pass ran
        (even if it repaired nothing); ``kind="unavailable"`` when no
        summarizer could be resolved. **Never raises** — per-target
        exceptions become ``skipped`` entries; a summarizer-factory
        failure becomes the ``"unavailable"`` arm.
    """
    targets = load_doctor_targets(db, conversation_id)
    if not targets:
        # No broken summaries → nothing to repair. Return early WITHOUT
        # resolving a summarizer (resolution may be expensive / may hit
        # the network). Mirrors TS lines 57-67.
        return DoctorApplyResult(
            kind="applied",
            detected=0,
            repaired=0,
            unchanged=0,
            skipped=[],
            repaired_summary_ids=[],
        )

    resolved_summarize = _resolve_doctor_apply_summarize(
        config=config,
        deps=deps,
        summarize=summarize,
        runtime_config=runtime_config,
    )
    if resolved_summarize is None:
        # Mirrors TS lines 69-75 — no summarizer means doctor-apply
        # cannot do its job. Surface a friendly "unavailable" rather
        # than raising.
        return DoctorApplyResult(
            kind="unavailable",
            reason=(
                "Lossless Hermes could not resolve a summarizer for native "
                "doctor apply through the normal model/auth chain."
            ),
        )

    ordered = _order_doctor_targets(db, conversation_id, targets)
    overrides: dict[str, _SummaryOverride] = {}
    skipped: list[dict[str, str]] = []
    repaired_summary_ids: list[str] = []
    unchanged = 0
    timezone_name = _config_timezone(config)

    for target in ordered:
        try:
            source_text = _build_summary_source_text(
                db=db,
                target=target,
                timezone_name=timezone_name,
                overrides=overrides,
            )
            if not source_text.strip():
                skipped.append({
                    "summary_id": target.summary_id,
                    "reason": "source text resolved empty",
                })
                continue

            previous_summary = _resolve_previous_summary_context(
                db=db,
                target=target,
                overrides=overrides,
            )

            options = LcmSummarizeOptions(
                previous_summary=previous_summary,
                is_condensed=_is_condensed_target(target),
                # TS forwards `depth` only on the condensed branch
                # (lines 107-108). The leaf branch leaves it unset; an
                # unset depth is harmless on the leaf path (the leaf
                # prompt builder ignores it).
                depth=target.depth if _is_condensed_target(target) else None,
            )
            rewritten = resolved_summarize(source_text, False, options).strip()
            if not rewritten:
                skipped.append({
                    "summary_id": target.summary_id,
                    "reason": "summarizer returned empty output",
                })
                continue
            if detect_doctor_marker(rewritten) is not None:
                # The summarizer produced output that STILL has a doctor
                # marker (e.g. a deterministic fallback because the
                # provider keeps failing). Skip rather than overwrite —
                # overwriting would let the doctor "repair" a row into
                # an identically-broken row on every run. Mirrors TS
                # lines 117-123.
                skipped.append({
                    "summary_id": target.summary_id,
                    "reason": "rewritten content still contains a doctor marker",
                })
                continue
            existing = target.content.strip() if isinstance(target.content, str) else ""
            if rewritten == existing:
                # Re-summarizing produced byte-identical content — no
                # write needed. Mirrors TS lines 124-127.
                unchanged += 1
                continue

            token_count = estimate_tokens(rewritten)
            overrides[target.summary_id] = _SummaryOverride(
                content=rewritten,
                token_count=token_count,
            )
            repaired_summary_ids.append(target.summary_id)
        except Exception as error:  # noqa: BLE001 — per-target isolation
            # A per-target failure (bad source-text join, summarizer
            # raising a non-auth error, etc.) is captured as a skip so
            # the rest of the pass continues. Mirrors the TS
            # `catch (error)` at lines 135-140.
            skipped.append({
                "summary_id": target.summary_id,
                "reason": str(error) if str(error) else "unknown repair failure",
            })

    if repaired_summary_ids:
        # All writes happen in ONE BEGIN IMMEDIATE block — a failure
        # part-way through the write loop rolls back EVERY write, so the
        # DB is never left with a partial repair. Mirrors TS lines
        # 143-160 (`withDatabaseTransaction(db, "BEGIN IMMEDIATE", ...)`).
        def _write_all() -> None:
            for summary_id in repaired_summary_ids:
                override = overrides.get(summary_id)
                if override is None:  # pragma: no cover - defensive
                    continue
                db.execute(
                    """
                    UPDATE summaries
                       SET content = ?, token_count = ?
                     WHERE summary_id = ?
                    """,
                    (override.content, override.token_count, summary_id),
                )
                _update_summary_fts(db, summary_id, override.content)

        with_database_transaction(db, "BEGIN IMMEDIATE", _write_all)

    return DoctorApplyResult(
        kind="applied",
        detected=len(targets),
        repaired=len(repaired_summary_ids),
        unchanged=unchanged,
        skipped=skipped,
        repaired_summary_ids=repaired_summary_ids,
    )


# ---------------------------------------------------------------------------
# Summarizer resolution
# ---------------------------------------------------------------------------


def _resolve_doctor_apply_summarize(
    *,
    config: Any,
    deps: Optional[SummarizerDeps],
    summarize: Optional[DoctorSummarizeFn],
    runtime_config: Any,
) -> Optional[DoctorSummarizeFn]:
    """Resolve the summarizer doctor-apply will use.

    Ports TS ``resolveDoctorApplySummarize`` (``lcm-doctor-apply.ts:172-194``).

    Resolution order:

    1. If ``summarize`` is callable, use it directly (the test seam +
       caller-supplied override).
    2. Else if ``deps`` is :data:`None`, return :data:`None` — there is
       no way to build a summarizer (the TS ``if (!params.deps) return
       undefined`` guard).
    3. Else construct an :class:`~lossless_hermes.summarize.LcmSummarizer`
       from ``deps`` + ``config`` and return its bound ``summarize``
       method. The Python :class:`~lossless_hermes.summarize.LcmSummarizer`
       constructor IS the port of the TS
       ``createLcmSummarizeFromLegacyParams`` factory.

    Any exception from :class:`~lossless_hermes.summarize.LcmSummarizer`
    construction (e.g. a malformed config) is swallowed and resolves to
    :data:`None` — the caller turns that into the ``"unavailable"``
    result arm. This mirrors the TS behavior where a factory that
    returns ``undefined`` (or whose returned object has no ``fn``)
    yields the ``"unavailable"`` branch.

    Args:
        config: :class:`~lossless_hermes.db.config.LcmConfig` (or
            mapping). Provides ``custom_instructions`` for the
            summarizer.
        deps: Optional :class:`~lossless_hermes.summarize.SummarizerDeps`.
        summarize: Optional explicit callable — highest priority.
        runtime_config: Host-runtime config forwarded for parity.
            Currently unused by the Python
            :class:`~lossless_hermes.summarize.LcmSummarizer` (which
            reads provider/model from ``config`` + env directly), but
            accepted so the resolution surface matches the TS factory's
            ``legacyParams.config`` input.

    Returns:
        A :data:`DoctorSummarizeFn`, or :data:`None` when no summarizer
        can be resolved.
    """
    if callable(summarize):
        return summarize
    if deps is None:
        return None

    # `runtime_config` is accepted for TS parity. The Python
    # LcmSummarizer derives provider/model from `config` + env, so there
    # is no separate runtime-config plumbing — referencing it here keeps
    # the parameter live for static analysis and documents intent.
    _ = runtime_config

    try:
        summarizer = LcmSummarizer(
            deps=deps,
            config=config,
            custom_instructions=_config_custom_instructions(config),
        )
    except Exception:  # noqa: BLE001 — factory failure → "unavailable"
        # TS: a factory that fails to produce a usable `fn` collapses to
        # the "unavailable" result arm. Mirror that — never let a
        # construction error escape doctor-apply.
        logger.warning(
            "[lcm] doctor apply: summarizer construction failed; reporting unavailable",
            exc_info=True,
        )
        return None

    if not summarizer.candidates:
        # No (provider, model) candidates resolved — `summarize()` would
        # raise on the first call. Treat exactly like a failed factory:
        # the caller surfaces "unavailable". Mirrors the TS path where
        # `createLcmSummarizeFromLegacyParams` returns `undefined` when
        # the chain is empty.
        return None

    return summarizer.summarize


# ---------------------------------------------------------------------------
# Target classification + ordering
# ---------------------------------------------------------------------------


def _is_condensed_target(target: DoctorTargetRecord) -> bool:
    """Whether ``target`` is a condensed summary (vs a leaf).

    Ports TS ``isCondensedTarget`` (``lcm-doctor-apply.ts:196-198``):
    ``return !(target.depth === 0 || target.kind === "leaf")``.

    A target is a leaf if EITHER its depth is 0 OR its kind is
    ``"leaf"``; anything else is condensed. Both conditions are checked
    (not just ``kind``) because depth-0 is the structural leaf marker
    and ``kind`` is the explicit one — they should always agree, but the
    TS guards both, so the port does too.
    """
    return not (target.depth == 0 or target.kind == "leaf")


def _order_doctor_targets(
    db: sqlite3.Connection,
    conversation_id: int,
    targets: list[DoctorTargetRecord],
) -> list[DoctorTargetRecord]:
    """Order targets for repair: active leaves, orphan leaves, condensed.

    Ports TS ``orderDoctorTargets`` (``lcm-doctor-apply.ts:200-228``).

    Three buckets, concatenated in this order:

    1. **Active leaves** — leaves that have a ``context_items`` row
       (loaded by :func:`_load_doctor_leaf_ordinals`). Sorted by their
       ``context_items.ordinal`` ASC, so they're repaired in prompt
       order.
    2. **Orphan leaves** — leaves with a marker but NO ``context_items``
       row (evicted from the live context but still in ``summaries``).
       Sorted by :func:`_compare_doctor_targets` (``depth``,
       ``created_at``, ``summary_id``).
    3. **Condensed summaries** — sorted by the same comparator.

    The leaves-before-condensed ordering is load-bearing for
    correctness (see module docstring).

    Args:
        db: Open connection — queried for the leaf ``context_items``
            ordinals.
        conversation_id: The conversation being repaired.
        targets: The unordered :func:`load_doctor_targets` output.

    Returns:
        ``targets`` re-ordered into the repair sequence.
    """
    leaf_ordinals = _load_doctor_leaf_ordinals(db, conversation_id)
    active_leaves: list[tuple[int, DoctorTargetRecord]] = []
    orphan_leaves: list[DoctorTargetRecord] = []
    condensed: list[DoctorTargetRecord] = []

    for target in targets:
        if not _is_condensed_target(target):
            context_ordinal = leaf_ordinals.get(target.summary_id)
            if context_ordinal is not None:
                active_leaves.append((context_ordinal, target))
            else:
                orphan_leaves.append(target)
            continue
        condensed.append(target)

    # Active leaves sort by their context_items.ordinal (the first
    # tuple element). Mirrors TS `activeLeaves.sort((l, r) =>
    # l.contextOrdinal - r.contextOrdinal)`.
    active_leaves.sort(key=lambda pair: pair[0])
    orphan_leaves.sort(key=_compare_doctor_targets_key)
    condensed.sort(key=_compare_doctor_targets_key)

    return [pair[1] for pair in active_leaves] + orphan_leaves + condensed


def _compare_doctor_targets_key(target: DoctorTargetRecord) -> tuple[int, str, str]:
    """Sort key mirroring TS ``compareDoctorTargets`` (lines 230-238).

    The TS comparator sorts by ``depth`` ASC, then ``created_at``
    lexicographically, then ``summary_id`` lexicographically. A
    3-tuple sort key reproduces that total order exactly — Python's
    tuple comparison is lexicographic over the elements, and ``str``
    comparison matches the TS ``String.prototype.localeCompare`` for the
    ASCII/ISO-8601 values these fields hold.
    """
    return (target.depth, target.created_at, target.summary_id)


def _load_doctor_leaf_ordinals(
    db: sqlite3.Connection,
    conversation_id: int,
) -> dict[str, int]:
    """Map ``summary_id`` → ``context_items.ordinal`` for broken leaves.

    Ports TS ``loadDoctorLeafOrdinals`` (``lcm-doctor-apply.ts:240-261``).

    Selects every depth-0 summary referenced by ``context_items`` for
    the conversation, then keeps ONLY the rows whose ``content`` still
    has a doctor marker (re-checked via
    :func:`~lossless_hermes.doctor.shared.detect_doctor_marker`). The
    returned map is consumed by :func:`_order_doctor_targets` to bucket
    "active" vs "orphan" leaves and to sort the active ones by prompt
    position.

    Args:
        db: Open connection.
        conversation_id: The conversation being repaired.

    Returns:
        ``{summary_id: ordinal}`` for broken depth-0 leaves that appear
        in ``context_items``. Leaves without a marker are excluded (a
        clean leaf is not a repair target).
    """
    cursor = db.execute(
        """
        SELECT ci.summary_id, ci.ordinal, COALESCE(s.content, '') AS content
          FROM context_items ci
          JOIN summaries s ON s.summary_id = ci.summary_id
         WHERE ci.conversation_id = ?
           AND ci.item_type = 'summary'
           AND COALESCE(s.depth, 0) = 0
         ORDER BY ci.ordinal ASC
        """,
        (conversation_id,),
    )
    ordinals: dict[str, int] = {}
    for row in cursor.fetchall():
        # 0: summary_id, 1: ordinal, 2: content.
        content = row[2] if row[2] is not None else ""
        if detect_doctor_marker(content) is None:
            # Clean leaf — not a repair target; skip. Mirrors TS
            # `if (!detectDoctorMarker(row.content ?? "")) continue`.
            continue
        ordinals[str(row[0])] = int(row[1])
    return ordinals


# ---------------------------------------------------------------------------
# Source-text construction
# ---------------------------------------------------------------------------


def _build_summary_source_text(
    *,
    db: sqlite3.Connection,
    target: DoctorTargetRecord,
    timezone_name: str,
    overrides: dict[str, _SummaryOverride],
) -> str:
    """Dispatch to the leaf or condensed source-text builder.

    Ports TS ``buildSummarySourceText`` (``lcm-doctor-apply.ts:263-272``).
    Condensed targets route to :func:`_build_condensed_source_text`;
    everything else (leaves) routes to :func:`_build_leaf_source_text`.
    """
    if _is_condensed_target(target):
        return _build_condensed_source_text(
            db=db,
            target=target,
            timezone_name=timezone_name,
            overrides=overrides,
        )
    return _build_leaf_source_text(db=db, target=target, timezone_name=timezone_name)


def _build_leaf_source_text(
    *,
    db: sqlite3.Connection,
    target: DoctorTargetRecord,
    timezone_name: str,
) -> str:
    """Reconstruct the raw-message source text for a leaf summary.

    Ports TS ``buildLeafSourceText`` (``lcm-doctor-apply.ts:274-295``).

    Joins ``summary_messages`` → ``messages`` for the leaf, then
    concatenates ``[<timestamp>]\\n<content>`` for each message in
    ``summary_messages.ordinal`` order, joined by blank lines. This
    reproduces the exact input the original leaf-pass summarizer saw, so
    re-summarizing produces a comparable summary.

    Args:
        db: Open connection.
        target: The leaf :class:`DoctorTargetRecord` being repaired.
        timezone_name: IANA timezone for the per-message timestamp
            header (from ``config.timezone``).

    Returns:
        The concatenated source text.

    Raises:
        ValueError: When the leaf has no linked messages — a structural
            problem the caller captures as a per-target skip (mirrors
            the TS ``throw new Error("no messages linked to summary")``).
    """
    cursor = db.execute(
        """
        SELECT m.created_at, COALESCE(m.content, '') AS content
          FROM summary_messages sm
          JOIN messages m ON m.message_id = sm.message_id
         WHERE sm.summary_id = ?
         ORDER BY sm.ordinal ASC
        """,
        (target.summary_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise ValueError("no messages linked to summary")

    parts = [
        f"[{_format_sqlite_timestamp(row[0], timezone_name)}]\n"
        f"{row[1] if row[1] is not None else ''}"
        for row in rows
    ]
    return "\n\n".join(parts)


def _build_condensed_source_text(
    *,
    db: sqlite3.Connection,
    target: DoctorTargetRecord,
    timezone_name: str,
    overrides: dict[str, _SummaryOverride],
) -> str:
    """Reconstruct the child-summary source text for a condensed summary.

    Ports TS ``buildCondensedSourceText`` (``lcm-doctor-apply.ts:297-349``).

    Joins ``summary_parents`` → ``summaries`` for each child of the
    condensed target, in ``summary_parents.ordinal`` order. For each
    child:

    * The child's content is read from the in-memory ``overrides`` map
      if it was rewritten earlier in THIS pass; otherwise from the DB
      row. **This is the override-propagation invariant** — leaves are
      repaired first, so by the time a condensed summary is processed
      its leaf children's rewrites are already in ``overrides``.
    * A ``[earliest - latest]`` time-range header is prepended when the
      child has resolvable timestamps.
    * Children whose (override-or-DB) content is empty after trimming
      are dropped.

    Args:
        db: Open connection.
        target: The condensed :class:`DoctorTargetRecord` being
            repaired.
        timezone_name: IANA timezone for the time-range header.
        overrides: The per-pass rewrite map — read for already-repaired
            children.

    Returns:
        The concatenated child-summary source text.

    Raises:
        ValueError: When the condensed summary has no linked children,
            or when every child resolved to empty content. Mirrors the
            two TS ``throw new Error(...)`` sites (lines 323-324,
            345-346); the caller captures it as a per-target skip.
    """
    cursor = db.execute(
        """
        SELECT
            sp.parent_summary_id AS summary_id,
            COALESCE(s.content, '') AS content,
            s.earliest_at,
            s.latest_at,
            s.created_at
          FROM summary_parents sp
          JOIN summaries s ON s.summary_id = sp.parent_summary_id
         WHERE sp.summary_id = ?
         ORDER BY sp.ordinal ASC
        """,
        (target.summary_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise ValueError("no child summaries linked to summary")

    parts: list[str] = []
    for row in rows:
        # 0: summary_id, 1: content, 2: earliest_at, 3: latest_at,
        # 4: created_at.
        child_summary_id = str(row[0])
        override = overrides.get(child_summary_id)
        raw_content = override.content if override is not None else row[1]
        # Mirror the TS coercion: a string is trimmed; a non-string
        # (defensive — should not happen with COALESCE) is stringified.
        if isinstance(raw_content, str):
            content = raw_content.strip()
        else:
            content = str(raw_content) if raw_content is not None else ""
        if not content:
            # Empty child contributes nothing — drop it. Mirrors the TS
            # `if (!content) return null` + `.filter(...)`.
            continue
        time_range = _resolve_summary_time_range(
            earliest_at=row[2],
            latest_at=row[3],
            created_at=row[4],
        )
        header = _format_summary_time_range(time_range, timezone_name)
        parts.append(f"{header}\n{content}" if header else content)

    if not parts:
        raise ValueError("child summaries resolved empty")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Previous-summary context resolution (three-fallback chain)
# ---------------------------------------------------------------------------


def _resolve_previous_summary_context(
    *,
    db: sqlite3.Connection,
    target: DoctorTargetRecord,
    overrides: dict[str, _SummaryOverride],
) -> Optional[str]:
    """Resolve the "previous summary" repair-context for ``target``.

    Ports TS ``resolvePreviousSummaryContext`` (``lcm-doctor-apply.ts:351-361``).

    Tries three fallbacks IN ORDER, returning the first that yields
    content:

    1. :func:`_previous_via_context_items` — the summary immediately
       before ``target`` in ``context_items`` order, at the same depth.
    2. :func:`_previous_via_summary_parents` — the sibling immediately
       before ``target`` in its parent's child list.
    3. :func:`_previous_via_timestamp` — the same-depth summary with the
       greatest ``created_at`` strictly before ``target``'s.

    The resolved text is forwarded to the summarizer as the
    :attr:`~lossless_hermes.summarize.LcmSummarizeOptions.previous_summary`
    option so the LLM has continuity context.

    Returns:
        The previous-summary content, or :data:`None` when all three
        fallbacks miss (a legitimate state — e.g. the first summary in
        a conversation has no predecessor).
    """
    return (
        _previous_via_context_items(db=db, target=target, overrides=overrides)
        or _previous_via_summary_parents(db=db, target=target, overrides=overrides)
        or _previous_via_timestamp(db=db, target=target, overrides=overrides)
    )


def _previous_via_context_items(
    *,
    db: sqlite3.Connection,
    target: DoctorTargetRecord,
    overrides: dict[str, _SummaryOverride],
) -> Optional[str]:
    """Fallback 1: previous summary via ``context_items`` ordinal.

    Ports TS ``previousViaContextItems`` (``lcm-doctor-apply.ts:363-400``).

    Looks up ``target``'s ``context_items.ordinal``, then finds the
    nearest summary-typed ``context_items`` row at a LOWER ordinal whose
    summary is at the same ``depth``. Returns that summary's content.

    Returns :data:`None` when ``target`` has no ``context_items`` row
    (it's an orphan), or when nothing precedes it at the same depth.
    """
    target_row = db.execute(
        """
        SELECT ordinal
          FROM context_items
         WHERE conversation_id = ?
           AND item_type = 'summary'
           AND summary_id = ?
         LIMIT 1
        """,
        (target.conversation_id, target.summary_id),
    ).fetchone()
    if target_row is None:
        return None
    target_ordinal = int(target_row[0])

    previous_row = db.execute(
        """
        SELECT s.summary_id
          FROM context_items ci
          JOIN summaries s ON s.summary_id = ci.summary_id
         WHERE ci.conversation_id = ?
           AND ci.item_type = 'summary'
           AND COALESCE(s.depth, 0) = ?
           AND ci.ordinal < ?
         ORDER BY ci.ordinal DESC
         LIMIT 1
        """,
        (target.conversation_id, target.depth, target_ordinal),
    ).fetchone()
    previous_summary_id = str(previous_row[0]) if previous_row is not None else None
    return _resolve_summary_content(db, previous_summary_id, overrides)


def _previous_via_summary_parents(
    *,
    db: sqlite3.Connection,
    target: DoctorTargetRecord,
    overrides: dict[str, _SummaryOverride],
) -> Optional[str]:
    """Fallback 2: previous summary via the ``summary_parents`` sibling chain.

    Ports TS ``previousViaSummaryParents`` (``lcm-doctor-apply.ts:402-430``).

    Finds a ``summary_parents`` row where ``target`` is the parent
    (i.e. ``target`` is consumed as a child by some rolled-up summary),
    reads that row's ``(summary_id, ordinal)``, then finds the sibling
    at a LOWER ``ordinal`` under the same rolled-up ``summary_id``.
    Returns that sibling-parent's content.

    Returns :data:`None` when ``target`` is not consumed as a child by
    anything, or has no lower-ordinal sibling.
    """
    parent_row = db.execute(
        """
        SELECT summary_id, ordinal
          FROM summary_parents
         WHERE parent_summary_id = ?
         LIMIT 1
        """,
        (target.summary_id,),
    ).fetchone()
    if parent_row is None:
        return None
    parent_child_summary_id = str(parent_row[0])
    parent_ordinal = int(parent_row[1])

    previous_row = db.execute(
        """
        SELECT parent_summary_id AS summary_id
          FROM summary_parents
         WHERE summary_id = ?
           AND ordinal < ?
         ORDER BY ordinal DESC
         LIMIT 1
        """,
        (parent_child_summary_id, parent_ordinal),
    ).fetchone()
    previous_summary_id = str(previous_row[0]) if previous_row is not None else None
    return _resolve_summary_content(db, previous_summary_id, overrides)


def _previous_via_timestamp(
    *,
    db: sqlite3.Connection,
    target: DoctorTargetRecord,
    overrides: dict[str, _SummaryOverride],
) -> Optional[str]:
    """Fallback 3: previous summary via ``created_at`` timestamp neighbor.

    Ports TS ``previousViaTimestamp`` (``lcm-doctor-apply.ts:432-459``).

    Finds the same-conversation, same-``depth`` summary with the
    greatest ``created_at`` strictly earlier than ``target``'s (ties
    broken by ``summary_id`` DESC, mirroring the TS ``(created_at < ?
    OR (created_at = ? AND summary_id < ?))`` predicate). Returns that
    summary's content.

    Returns :data:`None` when ``target`` has no ``created_at``, or when
    nothing precedes it.
    """
    created_at = target.created_at
    if not isinstance(created_at, str) or not created_at.strip():
        return None

    previous_row = db.execute(
        """
        SELECT summary_id
          FROM summaries
         WHERE conversation_id = ?
           AND COALESCE(depth, 0) = ?
           AND (created_at < ? OR (created_at = ? AND summary_id < ?))
         ORDER BY created_at DESC, summary_id DESC
         LIMIT 1
        """,
        (
            target.conversation_id,
            target.depth,
            created_at,
            created_at,
            target.summary_id,
        ),
    ).fetchone()
    previous_summary_id = str(previous_row[0]) if previous_row is not None else None
    return _resolve_summary_content(db, previous_summary_id, overrides)


def _resolve_summary_content(
    db: sqlite3.Connection,
    summary_id: Optional[str],
    overrides: dict[str, _SummaryOverride],
) -> Optional[str]:
    """Resolve a summary's content, preferring the in-pass override.

    Ports TS ``resolveSummaryContent`` (``lcm-doctor-apply.ts:461-480``).

    If ``summary_id`` was rewritten earlier in this pass, the trimmed
    override content is returned. Otherwise the DB row's trimmed content
    is returned. Either way an empty result resolves to :data:`None` (a
    blank previous-summary is no context at all).

    Args:
        db: Open connection.
        summary_id: The summary to resolve. :data:`None` → return
            :data:`None` immediately.
        overrides: The per-pass rewrite map.

    Returns:
        Non-empty trimmed content, or :data:`None`.
    """
    if not summary_id:
        return None

    override = overrides.get(summary_id)
    if override is not None and isinstance(override.content, str) and override.content.strip():
        return override.content.strip()

    row = db.execute(
        "SELECT COALESCE(content, '') AS content FROM summaries WHERE summary_id = ?",
        (summary_id,),
    ).fetchone()
    content = row[0].strip() if row is not None and isinstance(row[0], str) else ""
    return content if content else None


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _resolve_summary_time_range(
    *,
    earliest_at: Optional[str],
    latest_at: Optional[str],
    created_at: Optional[str],
) -> _SummaryTimeRange:
    """Resolve a condensed child's ``[earliest, latest]`` window.

    Ports TS ``resolveSummaryTimeRange`` (``lcm-doctor-apply.ts:482-493``).

    Each bound falls back to ``created_at`` when the dedicated column is
    missing or unparseable. Both bounds may still end up :data:`None`
    (e.g. all three inputs are blank); :func:`_format_summary_time_range`
    then emits an empty header.
    """
    earliest = _parse_sqlite_timestamp(earliest_at) or _parse_sqlite_timestamp(created_at)
    latest = _parse_sqlite_timestamp(latest_at) or _parse_sqlite_timestamp(created_at)
    return _SummaryTimeRange(earliest_at=earliest, latest_at=latest)


def _format_summary_time_range(time_range: _SummaryTimeRange, timezone_name: str) -> str:
    """Render a ``_SummaryTimeRange`` as a ``[earliest - latest]`` header.

    Ports TS ``formatSummaryTimeRange`` (``lcm-doctor-apply.ts:495-500``).

    Returns an empty string when either bound is :data:`None` — a
    condensed child with no resolvable timestamps gets no header line
    (the content alone is the source-text contribution).
    """
    if time_range.earliest_at is None or time_range.latest_at is None:
        return ""
    return (
        f"[{_format_timestamp(time_range.earliest_at, timezone_name)} - "
        f"{_format_timestamp(time_range.latest_at, timezone_name)}]"
    )


def _format_sqlite_timestamp(value: Any, timezone_name: str) -> str:
    """Format a SQLite timestamp string for a leaf-message header.

    Ports TS ``formatSqliteTimestamp`` (``lcm-doctor-apply.ts:502-509``).

    Parses ``value`` via :func:`_parse_sqlite_timestamp`; on success
    formats it through :func:`lossless_hermes.compaction._format_timestamp`
    (the SAME formatter the compaction leaf-pass uses, so re-summarized
    timestamps render identically). On a parse miss, returns the trimmed
    raw value, or the literal ``"unknown"`` when even that is empty.
    """
    parsed = _parse_sqlite_timestamp(value)
    if parsed is not None:
        return _format_timestamp(parsed, timezone_name)
    fallback = (
        value.strip() if isinstance(value, str) else (str(value) if value is not None else "")
    )
    return fallback or "unknown"


def _parse_sqlite_timestamp(value: Any) -> Optional[datetime]:
    """Parse a SQLite timestamp string into a tz-aware :class:`datetime`.

    Ports TS ``parseSqliteTimestamp`` (``lcm-doctor-apply.ts:511-527``).

    Two parse attempts, matching the TS ``new Date(...)`` fallbacks:

    1. Direct ISO-8601 parse (handles ``2026-04-22T14:35:00Z`` and
       ``2026-04-22T14:35:00+00:00`` forms).
    2. SQLite ``datetime('now')`` form (``2026-04-22 14:35:00``) — the
       space is replaced with ``T`` and a ``Z`` (UTC) suffix appended,
       mirroring the TS ``normalized.replace(" ", "T") + "Z"``.

    Returns :data:`None` for blank / non-string / unparseable input. A
    naive result is tagged UTC so downstream
    :func:`~lossless_hermes.compaction._format_timestamp` has a zone to
    convert from (the TS ``Date`` is always absolute-time).
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None

    parsed = _try_fromisoformat(normalized)
    if parsed is None:
        # SQLite `datetime('now')` form: "YYYY-MM-DD HH:MM:SS". Convert
        # the space separator to `T` and append `Z` so it parses as a
        # UTC instant. Mirrors TS line 522.
        parsed = _try_fromisoformat(normalized.replace(" ", "T") + "Z")
    if parsed is None:
        return None

    if parsed.tzinfo is None:
        # Naive timestamp — assume UTC (the conversation store tags
        # ingest times UTC; only test data is ever naive).
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _try_fromisoformat(value: str) -> Optional[datetime]:
    """Best-effort :meth:`datetime.fromisoformat` wrapper.

    :meth:`datetime.fromisoformat` rejects a trailing ``Z`` on Python <
    3.11; normalize it to ``+00:00`` first so the parse is uniform
    across the 3.11/3.12/3.13 CI matrix. Returns :data:`None` on any
    :class:`ValueError`.
    """
    candidate = value
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# FTS mirror update (best-effort)
# ---------------------------------------------------------------------------


def _update_summary_fts(db: sqlite3.Connection, summary_id: str, content: str) -> None:
    """Update the ``summaries_fts`` mirror for a repaired summary.

    Ports TS ``updateSummaryFts`` (``lcm-doctor-apply.ts:530-541``).

    Best-effort by design: ``summaries`` is the source of truth, the FTS
    table is a derived search index. The whole body is wrapped so a
    missing / stale-schema FTS table never aborts the repair
    transaction.

    Tries an ``UPDATE`` first; if it changed 0 rows (the summary has no
    FTS row yet) falls back to an ``INSERT``. Mirrors the TS
    ``update.changes === 0 → INSERT`` branch.

    Any :class:`sqlite3.Error` (no ``summaries_fts`` table, schema
    mismatch, etc.) is swallowed — the FTS index is rebuildable from
    ``summaries`` out-of-band. Mirrors the TS bare ``catch {}``.
    """
    try:
        cursor = db.execute(
            "UPDATE summaries_fts SET content = ? WHERE summary_id = ?",
            (content, summary_id),
        )
        if (cursor.rowcount or 0) == 0:
            db.execute(
                "INSERT INTO summaries_fts(summary_id, content) VALUES (?, ?)",
                (summary_id, content),
            )
    except sqlite3.Error:
        # FTS repair is best-effort; the primary source of truth is
        # `summaries`. A missing/stale FTS table must not fail the
        # repair. Mirrors the TS bare `catch {}` at lines 538-540.
        logger.debug(
            "[lcm] doctor apply: summaries_fts update skipped for %s",
            summary_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Config accessors (tolerant of LcmConfig OR a plain mapping)
# ---------------------------------------------------------------------------


def _config_timezone(config: Any) -> str:
    """Read ``config.timezone``, defaulting to ``"UTC"``.

    The ``config`` argument may be an
    :class:`~lossless_hermes.db.config.LcmConfig` (attribute access) or
    a plain mapping (used in tests). An empty / missing value resolves
    to ``"UTC"`` — :func:`~lossless_hermes.compaction._format_timestamp`
    treats ``"UTC"`` as the canonical default zone.
    """
    value = _config_get(config, "timezone", "")
    if isinstance(value, str) and value.strip():
        return value
    return "UTC"


def _config_custom_instructions(config: Any) -> Optional[str]:
    """Read ``config.custom_instructions``, normalizing empty → :data:`None`.

    Mirrors the TS ``params.config.customInstructions || undefined``
    coercion (``lcm-doctor-apply.ts:191``) — the
    :class:`~lossless_hermes.summarize.LcmSummarizer` expects
    :data:`None` (not an empty string) when there are no custom
    instructions.
    """
    value = _config_get(config, "custom_instructions", "")
    if isinstance(value, str) and value.strip():
        return value
    return None


def _config_get(config: Any, key: str, default: Any) -> Any:
    """Read ``key`` from ``config`` whether it's an object or a mapping.

    Tries :class:`~collections.abc.Mapping`-style ``config[key]`` first,
    then attribute access ``getattr(config, key, default)``. Returns
    ``default`` on a miss. Lets the public entry point accept either a
    real :class:`~lossless_hermes.db.config.LcmConfig` or a lightweight
    test double.
    """
    if hasattr(config, "get") and callable(config.get):
        try:
            return config.get(key, default)
        except TypeError:  # pragma: no cover - defensive
            pass
    return getattr(config, key, default)
