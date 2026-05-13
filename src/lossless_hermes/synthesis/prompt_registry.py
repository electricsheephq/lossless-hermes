"""Prompt registry — LCM v4.1 §3 / Group D (issue 07-08).

Versioned prompt templates per ``(memory_type, tier_label, pass_kind)``.
Append-only: old versions stay archived (``active=0``) for traceability
and so cache rows pointing at an archived ``prompt_id`` can still resolve
via :func:`get_prompt_by_id`. New versions are added with ``active=1``
and the previous-active row is flipped to ``active=0`` atomically in the
same transaction.

Schema lives in :sql:`lcm_prompt_registry` (created in issue 01-06, see
``src/lossless_hermes/db/migration.py`` ``_SQL_TABLE_LCM_PROMPT_REGISTRY``).

### Why versioning matters

Synthesis cache rows reference a ``prompt_id`` via
:sql:`lcm_synthesis_cache.prompt_id` (FK). When a prompt is updated,
cache invalidation can be SELECTIVE — only the entries that used the
superseded prompt need to be rebuilt. Bumping ``bundle_version`` (see
:func:`bump_bundle_version`) triggers voice-consistency rebuild across
the whole synthesis tier (when the prompt set is updated as a
coordinated unit).

### Lookup flow

Callers ask for the active prompt for a triple — :func:`get_active_prompt`
returns it. They also get the ``prompt_id`` which they pass into the
synthesis call so it is recorded on the cache row.

### Updates

:func:`register_prompt` deactivates the previous-active version (if
any) AND inserts the new version, all in a single transaction. ``version``
is auto-incremented (``max(version) + 1`` within the same triple,
counting active + archived rows).

### Source pin

* TS canonical: ``lossless-claw/src/synthesis/prompt-registry.ts``
  (commit ``1f07fbd`` on branch ``pr-613``, 305 LOC).
* Spec: ``epics/07-entity-synthesis/07-08-prompt-registry.md``.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from lossless_hermes.synthesis.types import (
    MemoryType,
    PassKind,
    PromptRecord,
)

__all__ = [
    "PromptRegistryError",
    "RegisterPromptOptions",
    "bump_bundle_version",
    "get_active_prompt",
    "get_prompt_by_id",
    "list_active_prompts",
    "register_prompt",
]


class PromptRegistryError(RuntimeError):
    """Raised by :func:`register_prompt` for surfaceable failure modes.

    Surfaced reasons:

    * ``"collision"`` — the auto-generated ``prompt_id`` (or a
      caller-supplied ``prompt_id_override``) hit a PK constraint
      violation. Bubbling up :class:`sqlite3.IntegrityError` would
      leak the storage layer; callers want a sentinel they can match
      against without importing ``sqlite3``.

    The ``reason`` is the first positional ``str`` arg (also exposed as
    ``args[0]`` per :class:`Exception` convention).
    """


@dataclass(frozen=True, slots=True)
class RegisterPromptOptions:
    """Options for :func:`register_prompt`.

    ``tier_label`` empty-string is normalized to ``None`` at the
    boundary — see Wave-9 Group D Gap 3 note in :func:`register_prompt`.
    """

    memory_type: MemoryType
    pass_kind: PassKind
    template: str
    tier_label: str | None = None
    model_recommendation: str | None = None
    bundle_version: int = 1
    notes: str | None = None
    prompt_id_override: str | None = None


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def get_active_prompt(
    db: sqlite3.Connection,
    *,
    memory_type: MemoryType,
    tier_label: str | None,
    pass_kind: PassKind,
) -> PromptRecord | None:
    """Return the currently-active prompt for the given triple, or ``None``.

    NULL ``tier_label`` is matched literally (i.e. ``tier_label=None``
    finds a row where the column ``IS NULL``, NOT a row where
    ``tier_label = ''``).

    If two rows are somehow active for the same triple (shouldn't
    happen thanks to ``lcm_prompt_registry_active_idx`` partial index,
    but defensive), returns the highest-version one.

    Args:
        db: Open :class:`sqlite3.Connection`. No transaction state
            required.
        memory_type: One of the six values in :data:`MemoryType`.
        tier_label: Tier name (``"daily"``, ``"weekly"`` etc.) or
            ``None`` / ``""`` for "no tier". Empty string normalizes to
            ``None`` per Wave-9 Group D Gap 3 (see :func:`register_prompt`).
        pass_kind: One of the three values in :data:`PassKind`.

    Returns:
        A :class:`PromptRecord` for the active row, or ``None`` if no
        prompt is registered for the triple.
    """

    # Wave-9 Group D Gap 3 (2026-03-08): normalize empty-string tier_label
    # to NULL. The migration's UNIQUE INDEX uses COALESCE(tier_label, '')
    # — treating NULL and '' as equivalent at the DB level. Aligning the
    # API surface here so callers don't get confusing "no row found"
    # results when they pass "" instead of None.
    normalized_tier = None if tier_label in (None, "") else tier_label

    if normalized_tier is None:
        sql = (
            "SELECT prompt_id, memory_type, tier_label, pass_kind, version, template,"
            " model_recommendation, created_at, active, bundle_version, notes"
            " FROM lcm_prompt_registry"
            " WHERE memory_type = ? AND tier_label IS NULL AND pass_kind = ?"
            "   AND active = 1"
            " ORDER BY version DESC LIMIT 1"
        )
        params: tuple[Any, ...] = (memory_type, pass_kind)
    else:
        sql = (
            "SELECT prompt_id, memory_type, tier_label, pass_kind, version, template,"
            " model_recommendation, created_at, active, bundle_version, notes"
            " FROM lcm_prompt_registry"
            " WHERE memory_type = ? AND tier_label = ? AND pass_kind = ?"
            "   AND active = 1"
            " ORDER BY version DESC LIMIT 1"
        )
        params = (memory_type, normalized_tier, pass_kind)

    row = db.execute(sql, params).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def get_prompt_by_id(
    db: sqlite3.Connection,
    prompt_id: str,
) -> PromptRecord | None:
    """Look up a prompt by exact ``prompt_id``.

    Used by synthesis-cache reads to verify the cache row's
    ``prompt_id`` is still current — or to recover the archived
    version that originally produced the cached text. **Does NOT
    filter on** ``active`` so it can resolve archived rows.

    Args:
        db: Open :class:`sqlite3.Connection`.
        prompt_id: Exact PK string (no normalization).

    Returns:
        :class:`PromptRecord` or ``None`` if the row does not exist.
    """

    sql = (
        "SELECT prompt_id, memory_type, tier_label, pass_kind, version, template,"
        " model_recommendation, created_at, active, bundle_version, notes"
        " FROM lcm_prompt_registry WHERE prompt_id = ?"
    )
    row = db.execute(sql, (prompt_id,)).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def list_active_prompts(db: sqlite3.Connection) -> list[PromptRecord]:
    """Return every active prompt (one per triple) for operator inspection.

    Used by ``/lcm health``. Ordering matches the TS implementation
    (``memory_type``, then ``COALESCE(tier_label, '')``, then
    ``pass_kind``) so operator-facing output is deterministic.

    Args:
        db: Open :class:`sqlite3.Connection`.

    Returns:
        List of :class:`PromptRecord` — one per active triple. Empty
        list if registry has not been seeded.
    """

    sql = (
        "SELECT prompt_id, memory_type, tier_label, pass_kind, version, template,"
        " model_recommendation, created_at, active, bundle_version, notes"
        " FROM lcm_prompt_registry WHERE active = 1"
        " ORDER BY memory_type, COALESCE(tier_label, ''), pass_kind"
    )
    return [_row_to_record(row) for row in db.execute(sql).fetchall()]


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def register_prompt(
    db: sqlite3.Connection,
    opts: RegisterPromptOptions,
) -> str:
    """Register a NEW prompt version. Returns the new ``prompt_id``.

    Opens ``BEGIN IMMEDIATE`` and performs three operations atomically:

    1. Compute ``max(version) + 1`` for the triple (across active +
       archived rows).
    2. Flip the previous-active row (if any) to ``active = 0``.
    3. Insert the new row with ``active = 1``.

    A PK collision (either auto-generated suffix collision or a
    caller-supplied ``prompt_id_override`` that already exists) raises
    :exc:`PromptRegistryError` with ``reason="collision"`` instead of
    bubbling :class:`sqlite3.IntegrityError` — callers want a sentinel
    they can match against without importing ``sqlite3``.

    Args:
        db: Open :class:`sqlite3.Connection`. **Must be in autocommit
            mode** (``isolation_level=None``) because the function
            issues its own ``BEGIN IMMEDIATE`` — Python's default
            ``isolation_level=""`` would inject an implicit ``BEGIN``
            on DML that conflicts.
        opts: :class:`RegisterPromptOptions` carrying the triple, the
            template, and any optional metadata.

    Returns:
        The newly-inserted ``prompt_id``. Either the caller's
        ``prompt_id_override`` or a generated ``pr_<6 hex chars>``
        string from :func:`secrets.token_hex`.

    Raises:
        PromptRegistryError: ``reason="collision"`` on PK collision.
        sqlite3.DatabaseError: Any other storage-layer failure
            (re-raised after ``ROLLBACK``).
    """

    # Wave-9 Group D Gap 3 (2026-03-08): normalize empty-string to NULL,
    # matching get_active_prompt + the COALESCE-based UNIQUE index in
    # migration.py.
    tier_label = None if opts.tier_label in (None, "") else opts.tier_label

    db.execute("BEGIN IMMEDIATE")
    try:
        # 1. Find current max version for this triple (across active + archived).
        if tier_label is None:
            max_row = db.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_v FROM lcm_prompt_registry"
                " WHERE memory_type = ? AND tier_label IS NULL AND pass_kind = ?",
                (opts.memory_type, opts.pass_kind),
            ).fetchone()
        else:
            max_row = db.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_v FROM lcm_prompt_registry"
                " WHERE memory_type = ? AND tier_label = ? AND pass_kind = ?",
                (opts.memory_type, tier_label, opts.pass_kind),
            ).fetchone()
        new_version = int(max_row[0]) + 1

        # 2. Deactivate the previous-active row (if any).
        if tier_label is None:
            db.execute(
                "UPDATE lcm_prompt_registry SET active = 0"
                " WHERE memory_type = ? AND tier_label IS NULL AND pass_kind = ?"
                "   AND active = 1",
                (opts.memory_type, opts.pass_kind),
            )
        else:
            db.execute(
                "UPDATE lcm_prompt_registry SET active = 0"
                " WHERE memory_type = ? AND tier_label = ? AND pass_kind = ?"
                "   AND active = 1",
                (opts.memory_type, tier_label, opts.pass_kind),
            )

        # 3. Insert the new active row.
        prompt_id = (
            opts.prompt_id_override
            if opts.prompt_id_override is not None
            else _generate_prompt_id()
        )
        try:
            db.execute(
                "INSERT INTO lcm_prompt_registry"
                " (prompt_id, memory_type, tier_label, pass_kind, version, template,"
                "  model_recommendation, active, bundle_version, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    prompt_id,
                    opts.memory_type,
                    tier_label,
                    opts.pass_kind,
                    new_version,
                    opts.template,
                    opts.model_recommendation,
                    opts.bundle_version,
                    opts.notes,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # PK or UNIQUE collision — surface a stable sentinel rather than
            # the storage exception class.
            db.execute("ROLLBACK")
            raise PromptRegistryError("collision") from exc

        db.execute("COMMIT")
        return prompt_id
    except PromptRegistryError:
        # ROLLBACK already issued above.
        raise
    except BaseException:
        # Roll back on any unexpected failure so the registry is left in
        # its pre-call state.
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
        raise


def bump_bundle_version(db: sqlite3.Connection) -> int:
    """Bump ``bundle_version`` on every active prompt atomically.

    Used by voice-consistency rebuilds after a coordinated prompt-set
    update. The single ``UPDATE`` is one SQL statement so it is
    inherently atomic — no explicit transaction needed (the surrounding
    connection's autocommit-or-implicit-tx semantics apply).

    Args:
        db: Open :class:`sqlite3.Connection`.

    Returns:
        The new ``bundle_version`` value (post-bump). Reads it from any
        active row after the UPDATE. ``0`` if the registry is empty
        (no active rows).
    """

    db.execute(
        "UPDATE lcm_prompt_registry SET bundle_version = bundle_version + 1 WHERE active = 1"
    )
    row = db.execute(
        "SELECT bundle_version FROM lcm_prompt_registry WHERE active = 1 LIMIT 1"
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _generate_prompt_id() -> str:
    """Return ``pr_<6 hex chars>`` from :func:`secrets.token_hex`.

    ~24 bits of entropy (~16M space). Matches the spec's prompt_id
    convention. Collisions surface as :exc:`PromptRegistryError`
    rather than being swallowed.
    """

    return f"pr_{secrets.token_hex(3)}"


def _row_to_record(row: Any) -> PromptRecord:
    """Materialize a :class:`PromptRecord` from a positional row tuple.

    Column order MUST match the SELECT lists above:
    ``(prompt_id, memory_type, tier_label, pass_kind, version, template,
       model_recommendation, created_at, active, bundle_version, notes)``.
    """

    return PromptRecord(
        prompt_id=row[0],
        memory_type=row[1],
        tier_label=row[2],
        pass_kind=row[3],
        version=int(row[4]),
        template=row[5],
        model_recommendation=row[6],
        created_at=row[7],
        active=bool(row[8]),
        bundle_version=int(row[9]),
        notes=row[10],
    )
