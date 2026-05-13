"""Entity coreference extraction tick — LCM v4.1 §6.1 / §7 Group E.

Ports ``lossless-claw/src/extraction/entity-coreference.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 498 LOC TS → ~600 LOC Python with
per-row SAVEPOINT scaffolding + Wave-N comments). The async worker job
drains :sql:`lcm_extraction_queue` once per call. For each queued leaf,
the injected extractor returns ``{surface, entityType}`` pairs which are
resolved against :sql:`lcm_entities` via the case-insensitive UNIQUE
index on ``(session_key, canonical_text COLLATE NOCASE)``; new entities
are inserted, existing ones get fresh ``lcm_entity_mentions`` rows.

### Invariants (lifted verbatim from the TS module docstring)

* Extraction is **async**, not inline with leaf write. v3.1 lesson:
  inline extraction couples gateway hot-path latency to LLM call
  latency. :sql:`lcm_extraction_queue` (added in migration A.03) is the
  inbox; this module drains it.
* Coreference simplification: exact-NOCASE matching only. Fuzzy /
  semantic coreference (voyage-3-lite entity-embedding KNN) is a
  follow-up.
* Type registry update: each extracted ``entity_type`` is upserted into
  :sql:`lcm_entity_type_registry` so operator tooling can review the
  type vocabulary.
* Idempotency: re-processing the same leaf is safe. ``mention_id`` is a
  deterministic ``men_<entity_id>_<leaf_id>_<surface_hash_for_id(surface)>``
  so repeated ticks ``INSERT OR IGNORE`` on the mention row and
  ``occurrence_count`` is only bumped on truly-new inserts (Wave-1 #7).

### Load-bearing Wave-N fixes preserved (per ADR-029)

* **Wave-1 (2025-11-08):** race-safe ``INSERT OR IGNORE`` against the
  ``(session_key, canonical_text COLLATE NOCASE)`` UNIQUE index. Two
  workers seeing the same canonical name simultaneously must not both
  abort their txn. See line marker below.
* **Wave-1 finding #2 (2025-11-08):** ``mention_id`` uses FNV-1a 32-bit
  hex over the FULL surface (not a 16-char truncation) so two surfaces
  sharing the first 16 chars no longer silently collide on the same
  ``(entity_id, leaf_id)`` bucket.
* **Wave-1 finding #3 (2025-11-08):** entity-id suffix uses 12 hex chars
  from a UUID (48 effective bits) — sufficient for realistic entity
  counts and avoids the 32-bit ``Math.random()`` birthday collision
  surface.
* **Wave-1 finding #7 (2025-11-08):** ``occurrence_count`` bumps ONLY on
  true new-mention insert (``rowcount > 0``), not unconditionally.
  Idempotent re-runs must not double-count.
* **Wave-4 P0-1 (2026-01-08):** per-item ``on_item_heartbeat`` callback;
  on ``False`` set ``lock_lost_mid_tick = True`` and ``break``. Do not
  attempt mid-tick recovery — the orchestrator decides next steps.
* **Wave-4 P1-1 (2026-01-08) + Wave-5 P1 (2026-02-04):** extractor
  throw bumps ``attempts`` + truncates ``last_error`` to 500 chars
  before storing. If the bump UPDATE itself fails (locked DB / schema
  race), record the secondary failure in ``itemDetail`` and try a
  bump-only retry so the dead-letter mechanism still progresses.
* **Wave-6 P2 (2026-02-22):** slice both halves to 500 chars before
  merging so a multi-MB error blob can't blow up ``result.per_item``.
* **Wave-7 P0 (2026-02-14):** **per-row SAVEPOINT** wrapping each
  ``{surface, entityType}`` resolution. A single bad surface (FK
  violation, encoding bomb, CHECK constraint) must not roll back the
  whole leaf's mentions. The SAVEPOINT name uses :func:`time.monotonic_ns`
  + an in-tick counter so back-to-back fast loops can't collide.
* **Wave-7 P1-E (2026-02-22):** fallback bump-only UPDATE so the
  dead-letter mechanism still progresses even if the first attempt
  (with ``last_error`` string) failed.
* **Wave-10 P2 (2026-03-22):** :func:`count_pending_extractions` selector
  parity — same ``kind``, same ``attempts < 5``, same
  ``suppressed_at IS NULL``. Mismatch caused autostart to spin on rows
  the tick would never select.

### Source map

* TS canonical: ``lossless-claw/src/extraction/entity-coreference.ts``
  (lines 1-498 at commit ``1f07fbd``).
* Porting guide: ``docs/porting-guides/entity-extraction.md`` §"Dequeue
  + worker loop" — the load-bearing algorithm description.
* Issue spec: ``epics/07-entity-synthesis/07-02-entity-coreference-worker.md``.
* ADR-029: ``docs/adr/029-wave-fix-provenance.md`` — Wave-N comment
  format.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Protocol, TypedDict

__all__ = [
    "DEFAULT_PER_TICK_LIMIT",
    "MAX_ATTEMPTS",
    "CoreferenceTickOptions",
    "CoreferenceTickResult",
    "ExtractEntitiesFn",
    "ExtractedEntity",
    "PerItemDetail",
    "count_pending_extractions",
    "run_coreference_tick",
    "surface_hash_for_id",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Default number of queue items processed per tick. Matches TS
#: ``DEFAULT_PER_TICK_LIMIT`` at ``entity-coreference.ts:125``. Callers
#: that need a tighter clamp (e.g. fast inner-loop tests) pass an explicit
#: ``per_tick_limit`` on :class:`CoreferenceTickOptions`.
DEFAULT_PER_TICK_LIMIT: int = 50

#: Dead-letter threshold. Queue rows with ``attempts >= MAX_ATTEMPTS`` are
#: skipped by BOTH selectors (this and :func:`count_pending_extractions`).
#:
#: LCM Wave-4 (2026-01-08): the schema had ``attempts`` + a dead-letter
#: partial index from migration.ts:1322-1335 but neither was used. Without
#: this gate, an extractor that keeps throwing on the same pathological
#: row burns the per-tick budget forever — and Wave-4 #4 noted the same
#: rows pile up under Voyage outages.
#: Original: lossless-claw/src/extraction/entity-coreference.ts:160.
MAX_ATTEMPTS: int = 5


# ---------------------------------------------------------------------------
# Public dataclasses + Protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedEntity:
    """One mention surface returned by the extractor.

    Mirrors the TS ``ExtractedEntity`` interface at
    ``entity-coreference.ts:45-66``.

    Attributes:
        surface: The surface form as it appears in the leaf text
            (e.g. ``"PR #71676"``). Stored verbatim in the mention row.
        entity_type: Free-text type label (e.g. ``"pr_number"``,
            ``"session_key"``, ``"agent_id"``). v4.1.1 §C: there is no
            CHECK constraint — operator domain has open-ended types.
            Each first-seen type is upserted into
            :sql:`lcm_entity_type_registry`.
        span_start: Optional offset of the surface in the source text;
            persisted in the mention row for future highlight / lineage.
        span_end: Optional end offset of the surface.
        canonical_text: Optional canonical text override. When ``None``
            (the default), defaults to ``surface.strip()``. The UNIQUE
            index uses ``COLLATE NOCASE`` so case variants dedupe
            automatically.
    """

    surface: str
    entity_type: str
    span_start: int | None = None
    span_end: int | None = None
    canonical_text: str | None = None


class ExtractEntitiesFn(Protocol):
    """Protocol for the entity-extractor callable.

    Mirrors the TS ``ExtractEntities`` type at
    ``entity-coreference.ts:68-72``. The Python signature is async (the TS
    side returns ``Promise<ExtractedEntity[]>``); the orchestrator owns
    the worker-lock heartbeat which is sync.

    The concrete implementations (LLM-backed, rule-based, deterministic
    test fakes) live in their own modules — issue 07-03 ports the
    Anthropic-API implementation. This worker module only knows the
    callable shape.
    """

    async def __call__(
        self,
        *,
        summary_id: str,
        session_key: str,
        content: str,
    ) -> list[ExtractedEntity]: ...


@dataclass
class CoreferenceTickOptions:
    """Per-tick configuration.

    Mirrors the TS ``CoreferenceTickOptions`` interface at
    ``entity-coreference.ts:74-97``.

    Attributes:
        pass_id: Caller-supplied identifier for the pass (audit /
            telemetry). Required.
        per_tick_limit: Maximum number of queue items to process this
            tick. Defaults to :data:`DEFAULT_PER_TICK_LIMIT`. The
            orchestrator picks a value that fits within the worker-lock
            TTL budget given LLM-call latency estimates.
        on_item_heartbeat: Optional per-item heartbeat callback. Caller
            supplies a function that extends the worker-lock TTL and
            returns whether we still hold it.

            **Returns ``False`` → caller has lost the lock; the tick
            aborts the loop, commits whatever's already done, and
            returns with :attr:`CoreferenceTickResult.lock_lost_mid_tick`
            set ``True``** so the orchestrator can adjust pacing.

            Kept synchronous at the signature level (see issue 07-02
            §Confidence "async-vs-sync shape"). The autostart wires a
            sync wrapper if its lock-info read goes through an awaitable.
    """

    pass_id: str
    per_tick_limit: int = DEFAULT_PER_TICK_LIMIT
    on_item_heartbeat: Callable[[], bool] | None = None


class PerItemDetail(TypedDict, total=False):
    """One per-queue-item diagnostic record.

    Mirrors the TS ``CoreferenceTickResult["perItem"][number]`` shape at
    ``entity-coreference.ts:114-122``. Marked ``total=False`` because
    ``entity_count``, ``mention_count``, and ``error`` are only set on
    the appropriate paths.
    """

    queue_id: str
    leaf_id: str
    success: bool
    entity_count: int
    mention_count: int
    error: str


@dataclass
class CoreferenceTickResult:
    """Aggregated telemetry returned from one tick.

    Mirrors the TS ``CoreferenceTickResult`` interface at
    ``entity-coreference.ts:99-123``.

    Attributes:
        processed_count: Number of queue items successfully drained.
        new_entities: Total entities newly inserted into
            :sql:`lcm_entities` across all leaves.
        new_mentions: Total mention rows newly inserted across all
            leaves.
        extractor_failures: Queue items where the extractor raised.
        lock_lost_mid_tick: Set ``True`` when ``on_item_heartbeat``
            returned ``False`` partway through the tick. The orchestrator
            (``tickExtraction`` in :mod:`worker_orchestrator`) MUST
            surface this so the autostart can adjust pacing — otherwise
            the next tick will repeat from a stale spot.
        per_item: Per-queue-item details for diagnostics. Each entry is
            a :class:`PerItemDetail` typed dict.
    """

    processed_count: int = 0
    new_entities: int = 0
    new_mentions: int = 0
    extractor_failures: int = 0
    lock_lost_mid_tick: bool = False
    per_item: list[PerItemDetail] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Selector parity helper
# ---------------------------------------------------------------------------


# Single shared selector predicate used by both the tick body and the
# pending-count probe. Keeping it in a constant makes the Wave-10 parity
# property self-evident in the source — any future change to one selector
# is impossible without touching this string.
#
# LCM Wave-10 (2026-03-22): `count_pending_extractions` selector parity.
# Previously the count probe only filtered on `kind` + `completed_at IS NULL`,
# but the tick selector also requires `attempts < MAX_ATTEMPTS` (dead-letter
# gate) AND `summaries.suppressed_at IS NULL` (don't process suppressed
# leaves). The mismatch caused the autostart loop to spin forever on rows
# the tick would never select — operator saw `pending_count > 0` but no
# progress.
# Original: lossless-claw/src/extraction/entity-coreference.ts:426-446.
_PENDING_PREDICATE = (
    "q.kind = ? AND q.completed_at IS NULL AND q.attempts < ? AND s.suppressed_at IS NULL"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _truncate(s: str, max_len: int = 500) -> str:
    """Truncate string to ``max_len`` chars (UTF-16 code units in JS terms).

    Python uses code-point slicing on ``str`` which differs from JS
    ``String.prototype.slice`` for surrogate-pair characters, but the
    error-message strings produced by ``str(exc)`` / ``exc.message`` here
    don't typically contain astral-plane glyphs. Keeping the contract at
    code-point granularity matches what an operator sees in tooling.

    LCM Wave-6 P2 (2026-02-22): bounded slice prevents a multi-MB error
    blob from blowing up ``result.per_item`` (which is returned to
    operators via /lcm health surfaces).
    Original: lossless-claw/src/extraction/entity-coreference.ts:228-232.
    """

    return s[:max_len]


def _random_entity_suffix() -> str:
    """Generate a collision-resistant entity-id suffix.

    LCM Wave-1 finding #3 (2025-11-08): JS ``Math.random()`` gives only
    32-bit space (~64K collision probability after 65K entities).
    Switched to ``crypto.randomUUID()`` prefix for ~128-bit collision-free
    space. We take 12 hex chars from a UUID — 48 bits, ~16M docs before
    birthday-collision becomes plausible. Sufficient for realistic entity
    counts (low millions max).
    Original: lossless-claw/src/extraction/entity-coreference.ts:451-468.

    Python ``uuid.uuid4()`` is RFC 4122 v4 (122 effective bits of
    randomness from ``os.urandom``); the 12-char hex prefix preserves
    the TS scheme byte-shape and collision properties exactly.
    """

    return uuid.uuid4().hex[:12]


# Regex used by surface_hash_for_id to sanitize the human-legible prefix.
# Compiled at import time so the per-mention path doesn't recompile per
# call (the tick processes up to 50 leaves × O(entities/leaf) per call —
# avoiding regex recompilation matters in tight inner loops).
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def surface_hash_for_id(surface: str, max_bytes: int = 16) -> str:
    """FNV-1a 32-bit hex hash of ``surface`` joined to a sanitized prefix.

    Byte-equivalent port of the TS ``surfaceHashForId`` at
    ``entity-coreference.ts:475-492``. The output format is
    ``<prefix>_<hex>`` (or just ``<hex>`` when ``max_bytes`` is too tight
    for any prefix room) where:

    * ``<hex>`` is the 8-char lowercase FNV-1a hash of the FULL surface
      (UTF-16 code units in JS / Python code points — see below).
    * ``<prefix>`` is the surface with every ``[^A-Za-z0-9]`` char
      replaced by ``_``, truncated to ``max_bytes - len(hex) - 1`` chars
      so the combined ``"<prefix>_<hex>"`` fits in ``max_bytes`` (the
      ``-1`` accounts for the joining underscore).

    LCM Wave-1 finding #2 (2025-11-08): the previous TS code took a
    16-char truncation of the surface, which produced intra-leaf
    collisions for surfaces sharing the first 16 alphanumerics (e.g.
    ``"PR #71676 (rebase target)"`` vs ``"PR #71676 (current)"``).
    Using a content hash of the FULL surface means collisions only happen
    on identical surfaces (the desired idempotency property — same surface
    in the same leaf for the same entity must produce the SAME mention_id
    so re-runs ``INSERT OR IGNORE`` cleanly no-op).
    Original: lossless-claw/src/extraction/entity-coreference.ts:475-492.

    **JS vs Python parity note.** TS uses ``charCodeAt`` which returns
    UTF-16 code units; Python ``ord(ch)`` over the ``str`` returns Unicode
    code points. For BMP characters (every byte we expect in entity
    surfaces — printable ASCII, common punctuation, occasional CJK) the
    two are identical. Surrogate-pair characters (astral plane glyphs,
    emoji) would diverge — TS would hash two halves separately while
    Python hashes the single code point. The vendored fixture parity test
    covers a CJK + a smiley to catch any regression here.

    Args:
        surface: The surface form to hash. May be empty.
        max_bytes: Maximum length of the returned id segment, including
            the hex tail and the joining underscore. Defaults to 16 to
            match the TS call site.

    Returns:
        A lowercase string of length ``min(len(surface_prefix) + 1 + 8,
        max_bytes)`` (or exactly 8 when ``max_bytes <= 9`` leaves no room
        for the prefix).

    The function is pure / total — never raises for any ``str`` input.
    """

    # FNV-1a 32-bit. Initial offset = 0x811c9dc5, prime = 0x01000193.
    # We mask to 32 bits after each multiply because Python integers are
    # arbitrary-width — without the mask the hash would grow unboundedly
    # and never match the TS ``Math.imul`` (which mod-2**32 implicitly).
    hash_val = 0x811C9DC5
    for ch in surface:
        hash_val ^= ord(ch)
        hash_val = (hash_val * 0x01000193) & 0xFFFFFFFF
    hex_part = f"{hash_val:08x}"

    # `max(0, ...)` mirrors the TS expression — if `max_bytes` is too
    # small for even one prefix char, we return just the hex (no leading
    # underscore). Matches the TS branch at line 491.
    prefix_len = max(0, max_bytes - len(hex_part) - 1)
    prefix = _NON_ALNUM_RE.sub("_", surface)[:prefix_len]
    return f"{prefix}_{hex_part}" if prefix else hex_part


def _make_savepoint_name(idx: int, counter: int) -> str:
    """Build a per-row SAVEPOINT name that is unique within and across ticks.

    LCM Wave-7 P0 (2026-02-14): SAVEPOINT names must be unique within
    the outer transaction. The TS source used ``Date.now().toString(36)``
    which has ms resolution and can collide on a fast loop running on
    macOS where ``time.time()`` is also ms-resolution (see issue 07-02
    §Confidence "SAVEPOINT-name millisecond collisions"). We use
    :func:`time.monotonic_ns` (nanosecond resolution) AND combine an
    in-tick ``counter`` token so back-to-back loops in the same tick can
    never collide even if the clock stalls.
    Original: lossless-claw/src/extraction/entity-coreference.ts:270.

    The base-36 encoding keeps the name short (SQLite SAVEPOINT names
    have no documented length limit but staying under 32 chars is
    polite). ``idx`` (the entity index within the leaf) plus
    ``counter`` (the global in-tick counter) plus the nanosecond
    timestamp give three orthogonal sources of uniqueness.
    """

    ns = time.monotonic_ns()
    # Python doesn't have a built-in base-36 formatter; manual loop is
    # ~3 lines and avoids a third-party dep. ``ns`` is at least 60 bits
    # of timestamp at any wall-clock past 2002, so 11+ base-36 chars.
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    n = ns
    if n == 0:
        s = "0"
    else:
        while n > 0:
            s = digits[n % 36] + s
            n //= 36
    return f"coref_{idx}_{counter}_{s}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_coreference_tick(
    db: sqlite3.Connection,
    extractor: ExtractEntitiesFn,
    opts: CoreferenceTickOptions,
) -> CoreferenceTickResult:
    """Drain :sql:`lcm_extraction_queue` once.

    Pulls up to ``opts.per_tick_limit`` queued ``kind='entity'`` rows
    ordered by ``queued_at ASC``, extracts entities from each leaf's
    content via the injected callable, and resolves them against
    :sql:`lcm_entities` using the case-insensitive UNIQUE index.

    The orchestrator (worker scheduler) handles cross-process lock
    acquisition + repeated ticks. **This function must NOT be called
    from the gateway hot path** — extraction is async-only per the v3.1
    invariant. The architectural invariant is enforced at the call site
    (the worker process); this module does not assert.

    Args:
        db: Open SQLite connection with the v4.1 schema applied. The
            connection's transaction mode matters: ``BEGIN IMMEDIATE``
            is issued per item, so the connection must be in autocommit
            (``isolation_level=None``) mode OR the caller must commit
            any pending transaction first. The standard library default
            (``isolation_level=""``) auto-injects BEGIN on DML which
            would conflict with our explicit ``BEGIN IMMEDIATE``.
        extractor: The injected entity-extractor callable. See
            :class:`ExtractEntitiesFn` for the protocol shape. Errors
            from the extractor are caught and surfaced via
            ``result.extractor_failures`` + ``per_item.error`` — they
            do not propagate.
        opts: Per-tick configuration. See :class:`CoreferenceTickOptions`.

    Returns:
        :class:`CoreferenceTickResult` with telemetry. Always returns
        normally; per-item failures are surfaced in the result rather
        than raised. Lock-loss mid-tick is signalled via
        :attr:`CoreferenceTickResult.lock_lost_mid_tick`.

    The function intentionally does NOT log; the orchestrator wires
    logging through the ``on_job_complete`` callback at the worker-loop
    layer (per :mod:`lossless_hermes.concurrency.worker_loop`).
    """

    per_tick_limit = opts.per_tick_limit
    result = CoreferenceTickResult()

    # In-tick counter token for SAVEPOINT name uniqueness; combined with
    # ``time.monotonic_ns()`` in `_make_savepoint_name` so back-to-back
    # iterations on the same nanosecond cannot collide.
    sp_counter = 0

    # 1. Pull queued items (kind='entity') ordered by queued_at ASC.
    #
    # LCM Wave-4 P1-1 (2026-01-08): dead-letter the queue rows that have
    # failed too many times. Without this gate, an extractor that keeps
    # throwing on the same pathological row burns the per-tick budget
    # forever (Wave-4 #4 noted the same rows pile up under Voyage outages).
    #
    # LCM Wave-10 P2 (2026-03-22): the predicate is identical to
    # `_PENDING_PREDICATE` so `count_pending_extractions` returns the
    # exact set this tick will draw.
    # Original: lossless-claw/src/extraction/entity-coreference.ts:161-178.
    queue_rows: list[tuple[str, str, int, str, str]] = list(
        db.execute(
            "SELECT q.queue_id, q.leaf_id, q.attempts, s.content, s.session_key "
            "FROM lcm_extraction_queue q "
            "JOIN summaries s ON s.summary_id = q.leaf_id "
            f"WHERE {_PENDING_PREDICATE} "
            "ORDER BY q.queued_at ASC "
            "LIMIT ?",
            ("entity", MAX_ATTEMPTS, per_tick_limit),
        )
    )

    for queue_id, leaf_id, _attempts, content, session_key in queue_rows:
        # LCM Wave-4 P0-1 (2026-01-08): heartbeat at the start of each item.
        # If lock-loss detected, abort the loop early and surface the signal.
        # Do NOT try to recover the lock mid-tick — the orchestrator decides.
        # Original: lossless-claw/src/extraction/entity-coreference.ts:182-189.
        if opts.on_item_heartbeat is not None:
            still_held = opts.on_item_heartbeat()
            if not still_held:
                result.lock_lost_mid_tick = True
                break

        item_detail: PerItemDetail = {
            "queue_id": queue_id,
            "leaf_id": leaf_id,
            "success": False,
        }

        # 2. Call the injected extractor. Errors here bump `attempts`
        #    + record `last_error` but do NOT mark the queue row done —
        #    the next tick will retry until `attempts >= MAX_ATTEMPTS`.
        try:
            extracted = await extractor(
                summary_id=leaf_id,
                session_key=session_key,
                content=content,
            )
        except BaseException as exc:  # noqa: BLE001 — extractor is foreign code.
            # Note: catching BaseException (not just Exception) matches the
            # TS `catch (e: unknown)` semantic. Synchronous SystemExit /
            # KeyboardInterrupt inside the extractor would be unusual but
            # we treat them the same as any other extractor failure rather
            # than crashing the whole tick (the worker-loop wrapper has its
            # own BaseException catch at the outer layer).
            err_msg = str(exc) if not isinstance(exc, str) else exc
            item_detail["error"] = _truncate(err_msg, 500)
            result.extractor_failures += 1

            # LCM Wave-4 P1-1 (2026-01-08) + Wave-5 P1 (2026-02-04):
            # bump attempts + record last_error so the dead-letter gate
            # actually fires after enough retries. If THIS UPDATE itself
            # fails (DB locked, schema race), record the secondary failure
            # in item_detail so callers can see the dead-letter mechanism
            # is broken — was previously silent + would loop forever.
            # Original: lossless-claw/src/extraction/entity-coreference.ts:219-248.
            try:
                db.execute(
                    "UPDATE lcm_extraction_queue "
                    "SET attempts = COALESCE(attempts, 0) + 1, "
                    "    last_error = ? "
                    "WHERE queue_id = ?",
                    (_truncate(err_msg, 500), queue_id),
                )
            except sqlite3.DatabaseError as update_exc:
                update_err_msg = str(update_exc)
                # LCM Wave-6 P2 (2026-02-22): slice both halves to 500
                # chars before merging so a multi-MB error blob can't
                # blow up result.per_item.
                # Original: lossless-claw/src/extraction/entity-coreference.ts:228-232.
                item_detail["error"] = (
                    f"{_truncate(err_msg, 500)} | "
                    f"dead-letter-update-failed: {_truncate(update_err_msg, 500)}"
                )
                # LCM Wave-7 P1-E (2026-02-22): fallback bump-only UPDATE
                # so the dead-letter mechanism still progresses even if the
                # first attempt (with last_error string) failed (e.g. due
                # to BLOB-size constraint or DB-locked retry). Without this,
                # attempts stays at 0 forever and the row retries indefinitely.
                # Original: lossless-claw/src/extraction/entity-coreference.ts:238-246.
                try:
                    db.execute(
                        "UPDATE lcm_extraction_queue "
                        "SET attempts = COALESCE(attempts, 0) + 1 "
                        "WHERE queue_id = ?",
                        (queue_id,),
                    )
                except sqlite3.DatabaseError:
                    # Best-effort. If even the simpler bump fails, the
                    # operator sees the "dead-letter-update-failed" string
                    # in item_detail and can manually purge the queue row
                    # via /lcm. Don't break the loop — other items may
                    # still be processable.
                    pass
            result.per_item.append(item_detail)
            continue

        # 3. Open per-item BEGIN IMMEDIATE outer transaction. Per-row
        #    SAVEPOINTs nest within (Wave-7). On any uncaught exception
        #    inside the body, ROLLBACK the whole leaf and continue with
        #    the next queue item — that item's other entities are NOT
        #    committed (acceptable per TS behavior: a leaf-level error
        #    means the whole batch for that leaf is unsafe to commit).
        entity_count_this_item = 0
        mention_count_this_item = 0
        db.execute("BEGIN IMMEDIATE")
        try:
            for entity_idx, ent in enumerate(extracted):
                # LCM Wave-7 P0 (2026-02-14): per-row SAVEPOINT inside
                # the batch tx so a SINGLE bad surface (FK violation,
                # encoding bomb, CHECK constraint failure, etc.) doesn't
                # ROLLBACK the whole leaf and discard its other valid
                # mentions. Without this, the dead-letter mechanism
                # (Wave-4) couldn't fire because the per-leaf
                # BEGIN IMMEDIATE / ROLLBACK wasn't bumping attempts —
                # leaving poison surfaces in infinite retry.
                # Original: lossless-claw/src/extraction/entity-coreference.ts:257-263.
                canonical = (
                    ent.canonical_text if ent.canonical_text is not None else ent.surface
                ).strip()
                if len(canonical) == 0:
                    continue
                sp_counter += 1
                sp_name = _make_savepoint_name(entity_idx, sp_counter)
                db.execute(f"SAVEPOINT {sp_name}")
                try:
                    # Upsert entity (using the NOCASE UNIQUE on
                    # (session_key, canonical_text)).
                    existing_row = db.execute(
                        "SELECT entity_id, occurrence_count FROM lcm_entities "
                        "WHERE session_key = ? AND canonical_text = ? COLLATE NOCASE",
                        (session_key, canonical),
                    ).fetchone()

                    if existing_row is not None:
                        entity_id = existing_row[0]
                        # LCM Wave-1 finding #7 (2025-11-08):
                        # occurrence_count is NOT bumped here. The bump
                        # happens below only on a truly-new mention insert
                        # (changes > 0). Idempotent re-processing must not
                        # double-count entities or mentions.
                        # Original: lossless-claw/src/extraction/entity-coreference.ts:287-292.
                    else:
                        # LCM Wave-1 finding #4 (2025-11-08): previous TS code
                        # did a plain INSERT, which threw UNIQUE constraint
                        # violation on concurrent ticks processing different
                        # leaves with the same canonical surface. ROLLBACK +
                        # retry forever was the result. Use INSERT OR IGNORE
                        # and re-SELECT to find the winner — race-safe.
                        # Original: lossless-claw/src/extraction/entity-coreference.ts:293-322.
                        entity_id = f"ent_{_random_entity_suffix()}"
                        cur = db.execute(
                            "INSERT OR IGNORE INTO lcm_entities "
                            "(entity_id, session_key, canonical_text, entity_type, "
                            " first_seen_at, last_seen_at, first_seen_in_summary_id, "
                            " occurrence_count) "
                            "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), ?, 0)",
                            (entity_id, session_key, canonical, ent.entity_type, leaf_id),
                        )
                        if cur.rowcount == 0:
                            # Lost the race — another concurrent tick won.
                            # Re-SELECT to find the winner.
                            winner = db.execute(
                                "SELECT entity_id FROM lcm_entities "
                                "WHERE session_key = ? AND canonical_text = ? COLLATE NOCASE",
                                (session_key, canonical),
                            ).fetchone()
                            if winner is not None:
                                entity_id = winner[0]
                            # If somehow not found, fall through — the
                            # mention INSERT will fail FK and the savepoint
                            # will roll back this entity's work. Safer than
                            # corrupting the catalog.
                        else:
                            entity_count_this_item += 1
                            # Update type registry (PK = type_name).
                            db.execute(
                                "INSERT INTO lcm_entity_type_registry "
                                "(type_name, first_seen_at, occurrence_count) "
                                "VALUES (?, datetime('now'), 1) "
                                "ON CONFLICT(type_name) DO UPDATE SET "
                                "  occurrence_count = occurrence_count + 1",
                                (ent.entity_type,),
                            )

                    # LCM Wave-1 finding #2 (2025-11-08): deterministic
                    # mention_id with FNV-1a content hash of the FULL surface
                    # (not 16-char truncation). Same surface in same leaf
                    # for same entity = SAME mention_id = INSERT OR IGNORE
                    # no-ops (correct idempotency). Different surfaces with
                    # shared 16-char prefix no longer silently collide.
                    # Original: lossless-claw/src/extraction/entity-coreference.ts:336-342.
                    mention_id = f"men_{entity_id}_{leaf_id}_{surface_hash_for_id(ent.surface, 16)}"
                    mention_cur = db.execute(
                        "INSERT OR IGNORE INTO lcm_entity_mentions "
                        "(mention_id, entity_id, summary_id, surface_form, "
                        " span_start, span_end, mentioned_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                        (
                            mention_id,
                            entity_id,
                            leaf_id,
                            ent.surface,
                            ent.span_start,
                            ent.span_end,
                        ),
                    )
                    if mention_cur.rowcount > 0:
                        mention_count_this_item += 1
                        # LCM Wave-1 finding #7 (2025-11-08): bump
                        # occurrence_count ONLY on truly-new mention insert.
                        # last_seen_at always advances (the latest leaf-write
                        # that mentions this entity is "seen now").
                        # Original: lossless-claw/src/extraction/entity-coreference.ts:358-368.
                        db.execute(
                            "UPDATE lcm_entities "
                            "SET occurrence_count = occurrence_count + 1, "
                            "    last_seen_at = datetime('now') "
                            "WHERE entity_id = ?",
                            (entity_id,),
                        )
                    # LCM Wave-7 P0 (2026-02-14): close the per-row SAVEPOINT.
                    # Releasing on success commits this entity's writes
                    # into the outer tx without affecting siblings.
                    # Original: lossless-claw/src/extraction/entity-coreference.ts:370-373.
                    db.execute(f"RELEASE {sp_name}")
                except (
                    sqlite3.DatabaseError,
                    sqlite3.IntegrityError,
                    sqlite3.OperationalError,
                ) as per_row_exc:
                    # LCM Wave-7 P0 (2026-02-14): per-surface failure
                    # rolls back JUST this entity's writes; siblings
                    # already-committed within the outer tx survive.
                    # Record the error in item_detail so operator /
                    # dead-letter sees partial-progress + which surface
                    # failed. Loop continues for other entities.
                    # Original: lossless-claw/src/extraction/entity-coreference.ts:374-391.
                    try:
                        db.execute(f"ROLLBACK TO {sp_name}")
                        db.execute(f"RELEASE {sp_name}")
                    except sqlite3.DatabaseError:
                        # Best-effort: if SAVEPOINT rollback itself fails,
                        # re-raise so the outer try/catch rolls back the
                        # whole leaf — the SAVEPOINT is unrecoverable.
                        raise per_row_exc from None
                    # Truncate at 200 chars to keep the merged error
                    # field readable when many entities fail per leaf.
                    per_row_msg = _truncate(str(per_row_exc), 200)
                    prev_err = item_detail.get("error", "")
                    item_detail["error"] = (
                        f"{prev_err} | per-row-failed[{entity_idx}]: {per_row_msg}"
                    )

            # 4. Mark queue row processed.
            db.execute(
                "UPDATE lcm_extraction_queue SET completed_at = datetime('now') WHERE queue_id = ?",
                (queue_id,),
            )
            db.execute("COMMIT")
            item_detail["success"] = True
            item_detail["entity_count"] = entity_count_this_item
            item_detail["mention_count"] = mention_count_this_item
            result.new_entities += entity_count_this_item
            result.new_mentions += mention_count_this_item
            result.processed_count += 1
        except BaseException as outer_exc:  # noqa: BLE001 — explicit catchall.
            # ROLLBACK the whole leaf transaction. The queue row is NOT
            # marked completed — the next tick will retry until
            # MAX_ATTEMPTS. We do NOT bump attempts here because this is
            # a per-leaf transactional failure (e.g. SAVEPOINT machinery
            # broke); the per-extractor-throw path above handles attempt
            # bumping for the "extractor genuinely failed" case.
            try:
                db.execute("ROLLBACK")
            except sqlite3.DatabaseError:
                # If the ROLLBACK itself fails, the connection is in an
                # unrecoverable state. Best to surface the rollback failure
                # and continue — the caller will likely see all later
                # items fail their BEGIN IMMEDIATE too.
                pass
            item_detail["error"] = f"tx-rollback: {_truncate(str(outer_exc), 500)}"
            result.extractor_failures += 1
        result.per_item.append(item_detail)

    return result


def count_pending_extractions(
    db: sqlite3.Connection,
    *,
    kind: Literal["entity", "procedure-recheck"] = "entity",
) -> int:
    """Probe :sql:`lcm_extraction_queue` for the number of pending items.

    For ``/lcm health`` and tick-scheduling decisions.

    LCM Wave-10 P2 (2026-03-22): the selector MUST match the tick
    selector exactly (same ``kind``, same ``attempts < MAX_ATTEMPTS``,
    same ``suppressed_at IS NULL``). Mismatch caused the autostart loop
    to spin on rows the tick would never select — operator saw
    ``pending_count > 0`` but no progress.
    Original: lossless-claw/src/extraction/entity-coreference.ts:421-446.

    Args:
        db: Open SQLite connection with the v4.1 schema applied.
        kind: Which queue kind to count. Defaults to ``"entity"``;
            ``"procedure-recheck"`` is a future hook for the procedure
            recheck worker (issue 07-N where N > 02).

    Returns:
        Non-negative count of queue rows that the next call to
        :func:`run_coreference_tick` (for this ``kind``) would draw from.
    """

    row = db.execute(
        f"SELECT COUNT(*) FROM lcm_extraction_queue q "
        f"JOIN summaries s ON s.summary_id = q.leaf_id "
        f"WHERE {_PENDING_PREDICATE}",
        (kind, MAX_ATTEMPTS),
    ).fetchone()
    # COUNT(*) is never NULL in SQLite; the cast satisfies ty's strict
    # mode (the row tuple is typed `tuple[Any, ...]`).
    return int(row[0])


# Awaitable / Callable convenience re-export pinned for ty's inference.
# Kept private so it doesn't widen the module's public surface.
_AwaitableExtract = Callable[..., Awaitable[list[ExtractedEntity]]]
