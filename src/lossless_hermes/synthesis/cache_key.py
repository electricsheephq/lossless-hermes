"""Synthesis cache key derivation + single-flight write — LCM v4.1 §3 (issue 07-06).

Centralises the 7-field UNIQUE-index composition that backs
:sql:`lcm_synthesis_cache`. Without these 7 fields in the lookup key,
two distinct production bugs surface:

* **Tier collision** — ``tier='custom'`` and ``tier='filtered'`` for the
  same ``(session_key, range, leaf_fingerprint)`` tuple would collapse
  onto the same cache row, silently returning wrong-tier text.
* **Stale-prompt service** — after :func:`prompt_registry.register_prompt`
  bumps the active prompt for a triple, the cache continued serving
  text from the previous prompt because the cache lookup ignored
  ``prompt_id``.

The Wave-10 fix (2026-03-22) widened both the UNIQUE index in
:func:`db.migration` and the cache lookup/write path here to seven
fields. The migration-side index was already expanded in epic 01-06;
this module ports the application-side composition + single-flight
INSERT semantics so callers cannot accidentally pick a different shape.

### The seven fields

==================  ============  ==============================================
Field               Type          Source / fallback
==================  ============  ==============================================
``session_key``     ``TEXT``      4-step fallback chain (see
                                  :func:`resolve_session_key`).
``range_start``     ``TEXT``      ISO-8601 UTC, lower window bound.
``range_end``       ``TEXT``      ISO-8601 UTC, upper window bound.
``leaf_fingerprint``  ``TEXT``    First 24 hex chars of
                                  ``SHA-256("\\0".join(leaf_ids))``;
                                  ORDER-SENSITIVE (see
                                  :func:`leaf_fingerprint`).
``grep_filter``     ``TEXT``      Pattern OR ``None``; ``None`` and
                                  ``""`` unified via
                                  ``COALESCE(grep_filter, '')`` in the
                                  UNIQUE index.
``tier_label``      ``TEXT``      One of ``daily / weekly / monthly /
                                  yearly / custom / filtered / year``.
``prompt_id``       ``TEXT``      Active-at-synthesis-time prompt FK.
==================  ============  ==============================================

### Single-flight write

The :func:`insert_cache_row_single_flight` helper wraps the
``INSERT OR IGNORE`` + ``SELECT``-back pattern. The UNIQUE index +
``OR IGNORE`` semantics give us cross-process single-flight: only one
caller's INSERT succeeds for any concrete 7-tuple. The losers' INSERT
no-ops (``changes=0``); they SELECT-back to find the in-flight cache
row and either join its wait or report ``building_elsewhere``.

### Source pin

* TS canonical: ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts``
  (commit ``1f07fbd`` on branch ``pr-613``, lines 550-557 for the
  fingerprint helper, 778-814 for the session_key fallback, 1184-1280
  for the INSERT/SELECT-back pair).
* Migration UNIQUE index: see
  :mod:`lossless_hermes.db.migration` (``lcm_synthesis_cache_lookup_uniq``).
* Spec: ``epics/07-entity-synthesis/07-06-synthesis-cache-key.md``.
* ADR-029: ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment
  format.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "DEFAULT_SESSION_KEY",
    "LEAF_FINGERPRINT_HEX_LEN",
    "CacheKey",
    "CacheRowInsertResult",
    "ExistingCacheRow",
    "InvalidLeafIdError",
    "generate_cache_id",
    "insert_cache_row_single_flight",
    "leaf_fingerprint",
    "lookup_cache_row",
    "resolve_session_key",
]


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


#: First-N hex-char truncation of the SHA-256 leaf fingerprint. 24 hex
#: chars == 96 bits — collision-free for any realistic cache lifetime.
#:
#: TS source: ``lcm-synthesize-around-tool.ts:556`` (``.slice(0, 24)``).
LEAF_FINGERPRINT_HEX_LEN: int = 24

#: Last-resort default session key for shell / CLI callers who don't carry
#: a session identity. Better to silo cache to a clear default than to
#: collapse to ``""`` and pollute across callers.
#:
#: TS source: ``lcm-synthesize-around-tool.ts:813``.
DEFAULT_SESSION_KEY: str = "agent:main:main"


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class InvalidLeafIdError(ValueError):
    """Raised by :func:`leaf_fingerprint` when a leaf_id contains a literal
    NUL byte.

    The fingerprint uses ``"\\0"`` as a separator between IDs; an ID
    that itself contains a NUL would make the fingerprint ambiguous
    (different ID lists could collide trivially). Schema-side, leaf_ids
    are ``s_<base36>`` strings so this should never happen in practice,
    but we guard early to fail loud rather than silently corrupt cache
    lookups.

    Residual-risk mitigation called out in the issue spec §"Confidence".
    """


# ---------------------------------------------------------------------------
# Fingerprint helper
# ---------------------------------------------------------------------------


def leaf_fingerprint(leaf_ids: Iterable[str]) -> str:
    """Compute the leaf-set fingerprint for cache-key composition.

    Returns the first :data:`LEAF_FINGERPRINT_HEX_LEN` (24) hex chars of
    ``SHA-256("\\0".join(leaf_ids))`` — equivalent to repeatedly feeding
    each ID then a NUL byte into the hasher.

    **ORDER-SENSITIVE.** ``leaf_fingerprint(["a", "b"])`` is NOT the same
    as ``leaf_fingerprint(["b", "a"])``. This matters because the leaf-
    selection path (time-window / period / semantic) produces IDs in a
    deterministic-but-mode-specific order; callers must not pre-sort the
    list before fingerprinting unless they own the ordering invariant.

    Raises:
        InvalidLeafIdError: If any leaf_id contains a literal NUL byte
            (``"\\0"``) which would make the fingerprint ambiguous.

    Args:
        leaf_ids: Iterable of leaf summary IDs. Order is preserved.

    Returns:
        24-hex-char SHA-256 prefix string.

    Example::

        >>> leaf_fingerprint(["s_alpha", "s_beta"])
        '8a04ef33...'  # actual: 24 hex chars

    Byte-for-byte parity with the TS source:
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:550-557``::

        function fingerprintLeaves(ids: string[]): string {
          const hash = createHash("sha256");
          for (const id of ids) {
            hash.update(id);
            hash.update("\\0");
          }
          return hash.digest("hex").slice(0, 24);
        }
    """

    hasher = hashlib.sha256()
    for leaf_id in leaf_ids:
        if "\0" in leaf_id:
            raise InvalidLeafIdError(
                f"leaf_id contains a literal NUL byte (fingerprint would be ambiguous): {leaf_id!r}"
            )
        hasher.update(leaf_id.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:LEAF_FINGERPRINT_HEX_LEN]


# ---------------------------------------------------------------------------
# Session-key resolution
# ---------------------------------------------------------------------------


def resolve_session_key(
    db: sqlite3.Connection,
    *,
    target_summary_session_key: str | None = None,
    input_session_key: str | None = None,
    conversation_ids: Iterable[int] = (),
) -> str:
    """Resolve the cache-row ``session_key`` via the Wave-7 P0 fallback chain.

    The 4-step fallback (TS source
    ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:775-814``):

    1. ``target_summary_session_key`` if non-empty after trim
    2. ``input_session_key`` if non-empty after trim
    3. ``SELECT session_key FROM conversations WHERE conversation_id = ?``
       for the first conversation in ``conversation_ids``, if non-empty
    4. :data:`DEFAULT_SESSION_KEY` (``"agent:main:main"``) as the safe
       default for shell/CLI callers who don't carry a session identity

    Wave-7 Auditor #6 P0 fix (2026-02-14): without the
    non-empty-key invariant, the UNIQUE cache index collapsed to ``""``
    for all such callers, causing CROSS-SESSION CACHE POLLUTION —
    caller A's cached synthesis surfaced in caller B's loser-path
    SELECT. The fallback chain guarantees the cache row's session_key
    is always meaningful at write-time.

    Args:
        db: Open :class:`sqlite3.Connection`. Used only for step 3
            (conversation lookup).
        target_summary_session_key: ``session_key`` from the anchor
            ``summaries`` row, if any.
        input_session_key: ``session_key`` from the request input, if
            any.
        conversation_ids: Iterable of conversation IDs in scope; step 3
            consults the first non-empty one.

    Returns:
        Resolved non-empty session_key string. Never ``""``.

    Example::

        >>> resolve_session_key(db, input_session_key="agent:claude-3:work-1")
        'agent:claude-3:work-1'
        >>> resolve_session_key(db)
        'agent:main:main'
    """

    # Step 1: target_summary.session_key
    if target_summary_session_key is not None:
        stripped = target_summary_session_key.strip()
        if stripped:
            return stripped

    # Step 2: input.session_key
    if input_session_key is not None:
        stripped = input_session_key.strip()
        if stripped:
            return stripped

    # Step 3: first conversation's session_key (DB lookup)
    for conv_id in conversation_ids:
        try:
            row = db.execute(
                "SELECT session_key FROM conversations WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
        except sqlite3.DatabaseError:
            # Defensive — TS source also swallows lookup errors silently
            # and falls through. The fallback chain still has a non-empty
            # tail in step 4.
            continue
        if row is None:
            continue
        candidate = row[0]
        if isinstance(candidate, str):
            stripped = candidate.strip()
            if stripped:
                return stripped

    # Step 4: last-resort default
    return DEFAULT_SESSION_KEY


# ---------------------------------------------------------------------------
# Cache-key dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheKey:
    """The 7-field cache lookup key for :sql:`lcm_synthesis_cache`.

    Frozen so the value cannot drift between INSERT and the SELECT-back
    on the loser path. All seven fields are required; the UNIQUE index
    on the table treats them as load-bearing for cross-process single-
    flight.

    LCM Wave-10 (2026-03-22): ``tier_label`` and ``prompt_id`` joined
    the cache key in this fix. Without them, ``tier='custom'`` then
    ``tier='filtered'`` for the same leaf set collided (silently
    returning wrong-tier text), and active-prompt updates silently
    served stale text from the old prompt_id.
    Original: ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1184-1198``.
    """

    session_key: str
    """The session this cache row belongs to. Always non-empty; resolved
    via :func:`resolve_session_key`."""

    range_start: str
    """ISO-8601 UTC lower bound for the cached synthesis window."""

    range_end: str
    """ISO-8601 UTC upper bound for the cached synthesis window."""

    leaf_fingerprint: str
    """24-hex-char SHA-256 prefix of the ordered leaf-ID list. Computed
    via :func:`leaf_fingerprint`."""

    grep_filter: str | None
    """Optional grep pattern, ``None`` for the un-filtered path. The
    UNIQUE index coalesces ``None`` and ``""`` to the empty string."""

    tier_label: str
    """One of ``daily / weekly / monthly / yearly / custom / filtered /
    year``. Constrained by the CHECK on
    :sql:`lcm_synthesis_cache.tier_label`."""

    prompt_id: str
    """Active-at-synthesis-time prompt FK pointing at
    :sql:`lcm_prompt_registry.prompt_id`."""


# ---------------------------------------------------------------------------
# Cache-id generator
# ---------------------------------------------------------------------------


def generate_cache_id() -> str:
    """Generate a fresh cache_id PK for ``lcm_synthesis_cache``.

    Uses :func:`secrets.token_hex` (12 bytes = 24 hex chars = 96 bits of
    entropy) which is collision-free for any realistic cache lifetime.
    The TS source uses a ``cache_around_<timestamp>_<6 hex>`` shape;
    we drop the prefix because the column is a PK with no human-
    readability requirement at the SQL layer.

    Returns:
        24-hex-char random string suitable for the ``cache_id`` PK.

    TS source: ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1043``.
    """

    return secrets.token_hex(12)


# ---------------------------------------------------------------------------
# Cache row INSERT (single-flight) + SELECT-back
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CacheRowInsertResult:
    """Result of an :func:`insert_cache_row_single_flight` call.

    Either we won the latch (the INSERT inserted a row) or we lost it
    to a concurrent caller (the INSERT no-op'd via OR IGNORE; the
    existing row's cache_id is reported).
    """

    cache_id: str
    """PK of the row that ended up in the table — either the caller's
    new ID (latch won) or the in-flight row's ID (latch lost)."""

    won_latch: bool
    """``True`` if THIS caller's INSERT succeeded (rowcount == 1).
    ``False`` if a concurrent caller already inserted a matching row
    (rowcount == 0); call :func:`lookup_cache_row` to inspect its
    status / content."""


def insert_cache_row_single_flight(
    db: sqlite3.Connection,
    *,
    cache_id: str,
    key: CacheKey,
    model_used: str,
    source_leaf_ids_json: str,
    source_token_count: int,
    actual_range_covered: str,
    leaf_count_synthesized: int,
    entity_index_json: str = "{}",
) -> CacheRowInsertResult:
    """INSERT a ``status='building'`` cache row via the UNIQUE-index latch.

    LCM Wave-10 (2026-03-22): the cache UNIQUE index keys on all 7 of
    :class:`CacheKey`'s fields — including ``tier_label`` and ``prompt_id``.
    Without those, ``tier='custom'`` then ``tier='filtered'`` for the
    same leaf set collided, and active-prompt updates silently served
    stale text. This INSERT participates in the same 7-tuple latch as
    the UNIQUE index; callers cannot accidentally pick a different
    shape.
    Original: ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1182-1224``.

    Cross-process single-flight semantics:

    * Two callers racing on the same :class:`CacheKey` both call this
      function. Only ONE's INSERT actually inserts (``rowcount == 1``);
      the other's INSERT no-ops via ``OR IGNORE`` (``rowcount == 0``).
    * The winner proceeds to call dispatch + UPDATE the row to
      ``status='ready'``.
    * The loser observes :attr:`CacheRowInsertResult.won_latch` ==
      ``False`` and calls :func:`lookup_cache_row` to find the winner's
      row (status / content).

    Args:
        db: Open :class:`sqlite3.Connection`. The caller controls the
            surrounding transaction (if any). The INSERT runs at the
            current transaction's isolation level.
        cache_id: Fresh PK. Generated via :func:`generate_cache_id`.
        key: The 7-field :class:`CacheKey` for this synthesis window.
        model_used: Model identifier used for the synthesis (recorded
            at row-write time; the dispatcher's actual-model audit may
            differ for the audit-row record).
        source_leaf_ids_json: JSON-encoded list of leaf summary IDs.
        source_token_count: Estimated input token count.
        actual_range_covered: Human-readable description of the
            actually-covered range (e.g. ``"2026-05-01..2026-05-02"``).
        leaf_count_synthesized: Number of leaves included in the
            synthesis. May be less than ``len(source_leaf_ids)`` if
            truncation occurred during ``buildSourceText``.
        entity_index_json: JSON-encoded entity index object. Defaults
            to ``"{}"``.

    Returns:
        :class:`CacheRowInsertResult` — :attr:`won_latch` indicates
        whether the caller should proceed to dispatch + UPDATE, or
        SELECT-back the existing row.
    """

    # LCM Wave-10 (2026-03-22): tier_label + prompt_id in cache UNIQUE index.
    # Without these, tier='custom' then tier='filtered' for the same leaf set
    # collided, and active-prompt updates silently served stale text.
    # The UNIQUE index in db/migration.py keys on
    # (session_key, range_start, range_end, leaf_fingerprint,
    #  COALESCE(grep_filter, ''), tier_label, prompt_id) — this INSERT
    # passes all 7 so OR IGNORE participates in the same latch shape.
    # Original: lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1184-1224.
    cursor = db.execute(
        "INSERT OR IGNORE INTO lcm_synthesis_cache"
        " (cache_id, session_key, range_start, range_end, leaf_fingerprint,"
        "  grep_filter, entity_index, model_used, prompt_id, tier_label,"
        "  source_leaf_ids, source_token_count, output_token_count,"
        "  actual_range_covered, leaf_count_synthesized,"
        "  status, building_started_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'building', datetime('now'))",
        (
            cache_id,
            key.session_key,
            key.range_start,
            key.range_end,
            key.leaf_fingerprint,
            key.grep_filter,
            entity_index_json,
            model_used,
            key.prompt_id,
            key.tier_label,
            source_leaf_ids_json,
            source_token_count,
            actual_range_covered,
            leaf_count_synthesized,
        ),
    )
    if cursor.rowcount == 1:
        return CacheRowInsertResult(cache_id=cache_id, won_latch=True)

    # rowcount == 0: another caller won the latch. Look up their row.
    existing = lookup_cache_row(db, key)
    if existing is None:
        # Pathological: OR IGNORE no-op'd but SELECT can't find the
        # winner (concurrent DELETE between INSERT and SELECT?). The
        # caller's downstream code will treat this as building_elsewhere
        # with an unknown cache_id; safer than asserting.
        return CacheRowInsertResult(cache_id="", won_latch=False)
    return CacheRowInsertResult(cache_id=existing.cache_id, won_latch=False)


@dataclass(frozen=True, slots=True)
class ExistingCacheRow:
    """Snapshot of an existing :sql:`lcm_synthesis_cache` row.

    Returned by :func:`lookup_cache_row` for the loser path of single-
    flight. Includes the fields needed to decide whether to return
    cached content (status='ready'), wait for an in-flight build
    (status='building'), or report a recent-failure (status='failed').
    """

    cache_id: str
    """PK of the existing row."""

    status: str
    """``'building'``, ``'ready'``, or ``'failed'``."""

    content: str | None
    """The synthesized output text, ``None`` until the winner UPDATEs
    the row to ``status='ready'``."""

    output_token_count: int
    """Token count of the output, ``0`` until the winner UPDATEs."""

    building_started_at: str | None
    """Wall-clock timestamp of the INSERT, for caller-side
    retry_after_ms computation."""

    failure_reason: str | None
    """Set when ``status='failed'``; surfaces upstream LLM failure
    cause."""


def lookup_cache_row(
    db: sqlite3.Connection,
    key: CacheKey,
) -> ExistingCacheRow | None:
    """SELECT-back the cache row matching a :class:`CacheKey`.

    Used by:

    1. **Loser-path single-flight** — caller's
       :func:`insert_cache_row_single_flight` returned
       :attr:`won_latch=False`; caller now looks up the winner's row to
       decide whether to wait, return cached, or surface a failure.
    2. **Direct cache hits** — caller wants to know if a ``status='ready'``
       row already exists for this 7-tuple before deciding to call
       :func:`insert_cache_row_single_flight` at all.

    LCM Wave-10 (2026-03-22): the WHERE clause matches the UNIQUE
    index's 7 fields, including ``tier_label`` and ``prompt_id``.
    The ``COALESCE(grep_filter, '') = COALESCE(?, '')`` parity with the
    index's ``COALESCE(grep_filter, '')`` expression keeps the SQLite
    planner using the index (and prevents NULL-vs-empty-string drift).
    Original: ``lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1254-1281``.

    Args:
        db: Open :class:`sqlite3.Connection`.
        key: The 7-field :class:`CacheKey` to look up.

    Returns:
        :class:`ExistingCacheRow` if a row matches all 7 fields, else
        ``None``. If multiple match (shouldn't happen — the index is
        UNIQUE) returns the most-recent ``building_started_at``.
    """

    # LCM Wave-10 (2026-03-22): tier_label + prompt_id in cache UNIQUE index.
    # WHERE clause matches the 7-field shape; COALESCE(grep_filter, '')
    # parity with the index lets SQLite use it.
    # Original: lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1254-1281.
    row = db.execute(
        "SELECT cache_id, status, content, output_token_count,"
        " building_started_at, failure_reason"
        " FROM lcm_synthesis_cache"
        " WHERE session_key = ? AND range_start = ? AND range_end = ?"
        "   AND leaf_fingerprint = ? AND COALESCE(grep_filter, '') = COALESCE(?, '')"
        "   AND tier_label = ? AND prompt_id = ?"
        " ORDER BY building_started_at DESC LIMIT 1",
        (
            key.session_key,
            key.range_start,
            key.range_end,
            key.leaf_fingerprint,
            key.grep_filter,
            key.tier_label,
            key.prompt_id,
        ),
    ).fetchone()
    if row is None:
        return None
    return ExistingCacheRow(
        cache_id=row[0],
        status=row[1],
        content=row[2],
        output_token_count=row[3] if row[3] is not None else 0,
        building_started_at=row[4],
        failure_reason=row[5],
    )
