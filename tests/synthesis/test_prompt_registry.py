"""Tests for :mod:`lossless_hermes.synthesis.prompt_registry` (issue 07-08).

Ports ``lossless-claw/test/synthesis-prompt-registry.test.ts`` (commit
``1f07fbd`` on branch ``pr-613``, 267 LOC) plus the Wave-9 Group D Gap 3
normalization invariants.

### Case mapping (TS → Python)

| TS describe block | Python class | Cases |
|---|---|---|
| registerPrompt + getActivePrompt | :class:`TestRegisterAndLookup` | 5 |
| registerPrompt with overrides | :class:`TestRegisterOverrides` | 2 |
| listActivePrompts | :class:`TestListActive` | 1 |
| bumpBundleVersion | :class:`TestBumpBundleVersion` | 2 |
| atomic transaction on registerPrompt | :class:`TestAtomicTransaction` | 1 |

### Additional Python-port tests (Wave-9 Group D Gap 3)

* :class:`TestEmptyStringTierNormalization` — register + lookup BOTH
  normalize ``""`` to ``None`` so callers don't get confusing
  "no row found" results.
* :class:`TestPromptIdFormat` — ``pr_<6 hex>`` from
  :func:`secrets.token_hex` (spec).
* :class:`TestCollisionRaisesPromptRegistryError` — PK collision
  surfaces as :exc:`PromptRegistryError`, not :class:`sqlite3.IntegrityError`.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.synthesis.prompt_registry import (
    PromptRegistryError,
    RegisterPromptOptions,
    bump_bundle_version,
    get_active_prompt,
    get_prompt_by_id,
    list_active_prompts,
    register_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_db() -> sqlite3.Connection:
    """Open an in-memory DB with FK enforcement + the v4.1 schema applied.

    Mirrors ``newDb()`` in ``synthesis-prompt-registry.test.ts:12-16``.
    ``isolation_level=None`` (autocommit) is required because
    :func:`register_prompt` issues its own ``BEGIN IMMEDIATE`` — Python's
    default ``isolation_level=""`` injects an implicit BEGIN on DML
    that would conflict.
    """

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    return conn


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB with seeding disabled."""

    conn = _new_db()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# registerPrompt + getActivePrompt
# ---------------------------------------------------------------------------


class TestRegisterAndLookup:
    """TS: ``synthesis-prompt-registry.test.ts:18-141``."""

    def test_register_first_returns_version_1_active(self, db: sqlite3.Connection) -> None:
        """TS line 19: first registration is ``version=1`` + ``active=true``."""
        prompt_id = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="Summarize:",
            ),
        )
        active = get_active_prompt(
            db, memory_type="episodic-leaf", tier_label=None, pass_kind="single"
        )
        assert active is not None
        assert active.prompt_id == prompt_id
        assert active.template == "Summarize:"
        assert active.version == 1
        assert active.active is True
        assert active.bundle_version == 1

    def test_register_second_version_flips_first_archived(self, db: sqlite3.Connection) -> None:
        """TS line 42: re-registering deactivates previous + bumps version."""
        v1 = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="weekly",
                pass_kind="single",
                template="v1 template",
            ),
        )
        v2 = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="weekly",
                pass_kind="single",
                template="v2 template",
            ),
        )
        assert v1 != v2

        active = get_active_prompt(
            db,
            memory_type="episodic-condensed",
            tier_label="weekly",
            pass_kind="single",
        )
        assert active is not None
        assert active.prompt_id == v2
        assert active.template == "v2 template"
        assert active.version == 2

        # v1 is still in the table (archived)
        v1_row = get_prompt_by_id(db, v1)
        assert v1_row is not None
        assert v1_row.active is False
        assert v1_row.template == "v1 template"

    def test_auto_versioning_is_per_triple(self, db: sqlite3.Connection) -> None:
        """TS line 75: distinct triples have independent versions."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf", pass_kind="single", template="leaf v1"
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf", pass_kind="single", template="leaf v2"
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="weekly",
                pass_kind="single",
                template="condensed v1",
            ),
        )

        leaf_active = get_active_prompt(
            db, memory_type="episodic-leaf", tier_label=None, pass_kind="single"
        )
        condensed_active = get_active_prompt(
            db,
            memory_type="episodic-condensed",
            tier_label="weekly",
            pass_kind="single",
        )
        assert leaf_active is not None
        assert condensed_active is not None
        assert leaf_active.version == 2
        assert condensed_active.version == 1

    def test_null_tier_label_is_matched_literally(self, db: sqlite3.Connection) -> None:
        """TS line 100: NULL tierLabel is matched literally (not coerced to '')."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                tier_label=None,
                pass_kind="single",
                template="no-tier",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                tier_label="monthly",
                pass_kind="single",
                template="monthly-tier",
            ),
        )

        no_tier = get_active_prompt(
            db, memory_type="episodic-leaf", tier_label=None, pass_kind="single"
        )
        monthly_tier = get_active_prompt(
            db, memory_type="episodic-leaf", tier_label="monthly", pass_kind="single"
        )
        assert no_tier is not None
        assert monthly_tier is not None
        assert no_tier.template == "no-tier"
        assert monthly_tier.template == "monthly-tier"

    def test_get_active_prompt_returns_none_when_unregistered(self, db: sqlite3.Connection) -> None:
        """TS line 131: returns null when no prompt is registered."""
        assert (
            get_active_prompt(
                db,
                memory_type="theme-consolidation",
                tier_label=None,
                pass_kind="single",
            )
            is None
        )

    def test_get_active_returns_highest_version_among_actives(self, db: sqlite3.Connection) -> None:
        """If two rows are active (shouldn't happen, but defensive), return the
        highest-version one. Emulated by toggling ``active`` manually."""
        v1 = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="v1",
            ),
        )
        v2 = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="v2",
            ),
        )
        # Force v1 back to active (simulate defensive case).
        db.execute("UPDATE lcm_prompt_registry SET active = 1 WHERE prompt_id = ?", (v1,))
        active = get_active_prompt(
            db, memory_type="episodic-leaf", tier_label=None, pass_kind="single"
        )
        assert active is not None
        assert active.prompt_id == v2  # highest version wins


# ---------------------------------------------------------------------------
# registerPrompt overrides
# ---------------------------------------------------------------------------


class TestRegisterOverrides:
    """TS: ``synthesis-prompt-registry.test.ts:144-177``."""

    def test_prompt_id_override_respected(self, db: sqlite3.Connection) -> None:
        """TS line 145: caller's ``prompt_id_override`` is used as the PK."""
        prompt_id = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="x",
                prompt_id_override="my-stable-id",
            ),
        )
        assert prompt_id == "my-stable-id"

    def test_stores_model_recommendation_bundle_version_notes(self, db: sqlite3.Connection) -> None:
        """TS line 157: model_recommendation, bundle_version, notes are stored."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="x",
                model_recommendation="claude-haiku-4-5",
                bundle_version=3,
                notes="test prompt",
            ),
        )
        active = get_active_prompt(
            db, memory_type="episodic-leaf", tier_label=None, pass_kind="single"
        )
        assert active is not None
        assert active.model_recommendation == "claude-haiku-4-5"
        assert active.bundle_version == 3
        assert active.notes == "test prompt"


# ---------------------------------------------------------------------------
# listActivePrompts
# ---------------------------------------------------------------------------


class TestListActive:
    """TS: ``synthesis-prompt-registry.test.ts:179-203``."""

    def test_returns_one_active_per_triple(self, db: sqlite3.Connection) -> None:
        """TS line 180: never returns archived rows."""
        register_prompt(
            db,
            RegisterPromptOptions(memory_type="episodic-leaf", pass_kind="single", template="v1"),
        )
        register_prompt(
            db,
            RegisterPromptOptions(memory_type="episodic-leaf", pass_kind="single", template="v2"),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="weekly",
                pass_kind="single",
                template="weekly v1",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-yearly",
                tier_label="2026",
                pass_kind="best_of_n_judge",
                template="yearly judge",
            ),
        )

        active = list_active_prompts(db)
        assert len(active) == 3
        # Leaf row is v2 (v1 archived).
        leaf_active = next(p for p in active if p.memory_type == "episodic-leaf")
        assert leaf_active.template == "v2"


# ---------------------------------------------------------------------------
# bumpBundleVersion
# ---------------------------------------------------------------------------


class TestBumpBundleVersion:
    """TS: ``synthesis-prompt-registry.test.ts:205-239``."""

    def test_bumps_active_rows_returns_new_value(self, db: sqlite3.Connection) -> None:
        """TS line 206: increments on every active prompt."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="x",
                bundle_version=1,
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="weekly",
                pass_kind="single",
                template="y",
                bundle_version=1,
            ),
        )
        assert bump_bundle_version(db) == 2

        leaf = get_active_prompt(
            db, memory_type="episodic-leaf", tier_label=None, pass_kind="single"
        )
        condensed = get_active_prompt(
            db,
            memory_type="episodic-condensed",
            tier_label="weekly",
            pass_kind="single",
        )
        assert leaf is not None
        assert condensed is not None
        assert leaf.bundle_version == 2
        assert condensed.bundle_version == 2

    def test_does_not_bump_archived(self, db: sqlite3.Connection) -> None:
        """TS line 231: archived rows are not bumped."""
        v1 = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="v1",
                bundle_version=5,
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="v2",
                bundle_version=1,
            ),
        )
        bump_bundle_version(db)
        v1_row = get_prompt_by_id(db, v1)
        assert v1_row is not None
        assert v1_row.bundle_version == 5  # unchanged

    def test_returns_zero_on_empty_registry(self, db: sqlite3.Connection) -> None:
        """When no active rows exist, returns 0 (post-bump value of a missing row)."""
        assert bump_bundle_version(db) == 0


# ---------------------------------------------------------------------------
# Atomic transaction
# ---------------------------------------------------------------------------


class TestAtomicTransaction:
    """TS: ``synthesis-prompt-registry.test.ts:241-266``."""

    def test_rollback_on_pk_collision(self, db: sqlite3.Connection) -> None:
        """TS line 242: collision on second insert rolls back the deactivation
        — v1 is still ``active=1`` afterwards."""
        v1 = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="v1",
                prompt_id_override="stable-id",
            ),
        )
        v1_row = get_prompt_by_id(db, v1)
        assert v1_row is not None
        assert v1_row.active is True

        # Try a second register with the SAME override — PK collision.
        with pytest.raises(PromptRegistryError) as exc_info:
            register_prompt(
                db,
                RegisterPromptOptions(
                    memory_type="episodic-leaf",
                    pass_kind="single",
                    template="v2 attempt",
                    prompt_id_override="stable-id",  # collision
                ),
            )
        assert exc_info.value.args[0] == "collision"

        # v1 should STILL be active (rollback restored it).
        v1_row_after = get_prompt_by_id(db, v1)
        assert v1_row_after is not None
        assert v1_row_after.active is True


# ---------------------------------------------------------------------------
# Empty-string tier_label normalization (Wave-9 Group D Gap 3)
# ---------------------------------------------------------------------------


class TestEmptyStringTierNormalization:
    """Verify Wave-9 Group D Gap 3: empty-string tier_label is normalized to
    ``None`` in BOTH :func:`register_prompt` and :func:`get_active_prompt`.

    The COALESCE UNIQUE index in migration.py treats NULL and '' as equal,
    so callers must NOT pass ``""`` and expect the wildcard match; this
    normalization happens at the Python boundary."""

    def test_register_empty_string_tier_treated_as_null(self, db: sqlite3.Connection) -> None:
        """``tier_label=""`` on register stores ``NULL`` in the DB."""
        prompt_id = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                tier_label="",  # empty string
                pass_kind="single",
                template="x",
            ),
        )
        row = db.execute(
            "SELECT tier_label FROM lcm_prompt_registry WHERE prompt_id = ?",
            (prompt_id,),
        ).fetchone()
        assert row is not None
        assert row[0] is None

    def test_lookup_empty_string_tier_finds_null_row(self, db: sqlite3.Connection) -> None:
        """A row written with ``tier_label=None`` is found by lookups using ``""``."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                tier_label=None,
                pass_kind="single",
                template="no-tier",
            ),
        )
        active = get_active_prompt(
            db,
            memory_type="episodic-leaf",
            tier_label="",  # empty string
            pass_kind="single",
        )
        assert active is not None
        assert active.template == "no-tier"


# ---------------------------------------------------------------------------
# prompt_id format
# ---------------------------------------------------------------------------


class TestPromptIdFormat:
    """Spec: auto-generated prompt_id is ``pr_<6 hex chars>`` from
    :func:`secrets.token_hex(3)`."""

    def test_auto_prompt_id_matches_format(self, db: sqlite3.Connection) -> None:
        """``pr_<6 hex chars>``."""
        prompt_id = register_prompt(
            db,
            RegisterPromptOptions(memory_type="episodic-leaf", pass_kind="single", template="x"),
        )
        assert re.fullmatch(r"pr_[0-9a-f]{6}", prompt_id), (
            f"prompt_id {prompt_id!r} does not match pr_<6 hex chars>"
        )

    def test_auto_prompt_ids_are_unique_across_calls(self, db: sqlite3.Connection) -> None:
        """Different triples → different prompt_ids."""
        a = register_prompt(
            db,
            RegisterPromptOptions(memory_type="episodic-leaf", pass_kind="single", template="x"),
        )
        b = register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="y",
            ),
        )
        assert a != b


# ---------------------------------------------------------------------------
# PromptRegistryError surface
# ---------------------------------------------------------------------------


class TestCollisionRaisesPromptRegistryError:
    """Spec: PK collision surfaces as :exc:`PromptRegistryError("collision")`,
    NOT :class:`sqlite3.IntegrityError`. Callers should not need to import
    ``sqlite3`` to catch this."""

    def test_collision_is_prompt_registry_error_not_sqlite_integrity_error(
        self, db: sqlite3.Connection
    ) -> None:
        """Override collision raises :exc:`PromptRegistryError`."""
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-leaf",
                pass_kind="single",
                template="x",
                prompt_id_override="dup-id",
            ),
        )

        # The catch is PromptRegistryError, NOT sqlite3.IntegrityError.
        # If the implementation regresses and raises IntegrityError, the
        # `pytest.raises(PromptRegistryError)` ctx will fail.
        with pytest.raises(PromptRegistryError) as exc_info:
            register_prompt(
                db,
                RegisterPromptOptions(
                    memory_type="episodic-leaf",
                    pass_kind="single",
                    template="y",
                    prompt_id_override="dup-id",
                ),
            )
        assert exc_info.value.args[0] == "collision"
        # And the wrapped __cause__ IS the sqlite3.IntegrityError (preserves
        # debugging trail without leaking the storage class).
        assert isinstance(exc_info.value.__cause__, sqlite3.IntegrityError)
