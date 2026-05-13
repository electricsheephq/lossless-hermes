"""Tests for :mod:`lossless_hermes.synthesis.seed_prompts` (issue 07-08).

Ports ``lossless-claw/test/v41-seed-default-prompts.test.ts`` (commit
``1f07fbd`` on branch ``pr-613``, 152 LOC) plus Wave-9 P1 placeholder
hygiene + SHA-256 byte-for-byte snapshot pins.

### Case mapping (TS → Python)

| TS describe block | Python test | Cases |
|---|---|---|
| seeds the §12 default prompts on an empty registry | :class:`TestSeedEmpty` | 1 |
| seeds the specific triples synthesize_around + dispatch require | :class:`TestSeedTriples` | 1 |
| idempotent — re-running does not duplicate or change | :class:`TestSeedIdempotent` | 1 |
| does NOT overwrite an operator-registered prompt | :class:`TestSeedOperatorOverride` | 1 |
| runs inside migration transaction without nested-tx error | :class:`TestSeedComposesWithMigration` | 1 |
| running migration twice stays idempotent | :class:`TestSeedComposesWithMigration` | 1 |

### Additional Python-port tests

* :class:`TestNoForbiddenPlaceholders` (Wave-9 P1) — every seeded
  template MUST NOT contain ``{{date_range}}`` or ``{{target_length}}``;
  the renderer doesn't substitute those, so they'd ship verbatim.
* :class:`TestSeedTemplateSnapshots` — SHA-256 byte-for-byte pin for
  each of the 11 templates. Cross-verified against TS source at port
  time (see ``hash_ts_prompts.mjs`` reference in PR description).
* :class:`TestSeedComposesWithBeginExclusive` — explicit assertion that
  ``seed_default_prompts`` works inside an outer ``BEGIN EXCLUSIVE``
  (the migration ladder shape).
* :class:`TestSeedPromptIdFormat` — every seeded prompt_id matches
  ``pr_<6 hex chars>``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.synthesis.prompt_registry import get_active_prompt
from lossless_hermes.synthesis.seed_prompts import (
    DEFAULT_PROMPTS,
    seed_default_prompts,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fresh_db_no_seed() -> sqlite3.Connection:
    """Migrated in-memory DB with seeding disabled (test controls seeding).

    Mirrors ``freshDb()`` in ``v41-seed-default-prompts.test.ts:7-13``.
    """

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    return conn


@pytest.fixture
def db_no_seed() -> Iterator[sqlite3.Connection]:
    """Migrated DB, registry empty."""

    conn = _fresh_db_no_seed()
    try:
        yield conn
    finally:
        conn.close()


def _sha256(s: str) -> str:
    """UTF-8 encode + hex digest."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# TS test parity
# ---------------------------------------------------------------------------


class TestSeedEmpty:
    """TS line 16: seeds the §12 default prompts on an empty registry."""

    def test_seed_on_empty_returns_nonzero_seeded_and_zero_skipped(
        self, db_no_seed: sqlite3.Connection
    ) -> None:
        before = db_no_seed.execute("SELECT COUNT(*) FROM lcm_prompt_registry").fetchone()
        assert before[0] == 0

        result = seed_default_prompts(db_no_seed)
        assert result.seeded > 0
        assert result.skipped == 0

        after = db_no_seed.execute("SELECT COUNT(*) FROM lcm_prompt_registry").fetchone()
        assert after[0] == result.seeded


class TestSeedTriples:
    """TS line 35: seeds the specific (memory_type, tier_label, pass_kind)
    triples that synthesize_around + dispatch require."""

    REQUIRED_TRIPLES: tuple[tuple[str, str | None, str], ...] = (
        ("episodic-leaf", None, "single"),
        ("episodic-condensed", "daily", "single"),
        ("episodic-condensed", "weekly", "single"),
        ("episodic-condensed", "monthly", "single"),
        ("episodic-condensed", "monthly", "verify_fidelity"),
        ("episodic-yearly", "yearly", "single"),
        ("episodic-yearly", "yearly", "best_of_n_judge"),
        ("episodic-condensed", "custom", "single"),
        ("episodic-condensed", "filtered", "single"),
        ("procedural-extract", None, "single"),
        ("entity-extract", None, "single"),
    )

    def test_all_11_required_triples_seeded(self, db_no_seed: sqlite3.Connection) -> None:
        """All 11 required triples present after seed; templates non-empty."""
        seed_default_prompts(db_no_seed)
        for memory_type, tier_label, pass_kind in self.REQUIRED_TRIPLES:
            found = get_active_prompt(
                db_no_seed,
                memory_type=memory_type,  # type: ignore[arg-type]
                tier_label=tier_label,
                pass_kind=pass_kind,  # type: ignore[arg-type]
            )
            assert found is not None, (
                f"expected seed for ({memory_type}, {tier_label}, {pass_kind})"
            )
            assert len(found.template) > 50

    def test_default_prompts_count_matches_required_triples(self) -> None:
        """:data:`DEFAULT_PROMPTS` covers exactly the 11 documented triples."""
        seeded_triples = {(p.memory_type, p.tier_label, p.pass_kind) for p in DEFAULT_PROMPTS}
        required = set(self.REQUIRED_TRIPLES)
        assert seeded_triples == required


class TestSeedIdempotent:
    """TS line 67: re-running the seed does not duplicate or change rows."""

    def test_two_consecutive_calls_second_is_no_op(self, db_no_seed: sqlite3.Connection) -> None:
        r1 = seed_default_prompts(db_no_seed)
        assert r1.seeded > 0
        assert r1.skipped == 0

        r2 = seed_default_prompts(db_no_seed)
        assert r2.seeded == 0
        assert r2.skipped == r1.seeded

        count = db_no_seed.execute("SELECT COUNT(*) FROM lcm_prompt_registry").fetchone()
        assert count[0] == r1.seeded


class TestSeedOperatorOverride:
    """TS line 82: an operator-registered prompt at the same triple is preserved."""

    def test_operator_override_not_clobbered(self, db_no_seed: sqlite3.Connection) -> None:
        # Operator manually registered a custom prompt for episodic-condensed/daily/single.
        db_no_seed.execute(
            "INSERT INTO lcm_prompt_registry"
            " (prompt_id, memory_type, tier_label, pass_kind, version, template,"
            "  model_recommendation, active, bundle_version, notes)"
            " VALUES (?, ?, ?, ?, 1, ?, ?, 1, 1, ?)",
            (
                "prompt_operator_override",
                "episodic-condensed",
                "daily",
                "single",
                "OPERATOR-OVERRIDE-TEMPLATE",
                "claude-opus-4-7",
                "operator override",
            ),
        )

        result = seed_default_prompts(db_no_seed)
        # Daily was already there → skipped; everything else seeded.
        assert result.skipped == 1
        assert result.seeded > 0

        # Operator's prompt is still active and unchanged.
        active = get_active_prompt(
            db_no_seed,
            memory_type="episodic-condensed",
            tier_label="daily",
            pass_kind="single",
        )
        assert active is not None
        assert active.prompt_id == "prompt_operator_override"
        assert active.template == "OPERATOR-OVERRIDE-TEMPLATE"
        assert active.model_recommendation == "claude-opus-4-7"


class TestSeedComposesWithMigration:
    """TS lines 119 + 136: the seed runs inside the migration's ``BEGIN
    EXCLUSIVE`` without a nested-tx error; running migration twice is
    idempotent."""

    def test_run_lcm_migrations_default_seeding_succeeds(self) -> None:
        """The production migration path (default ``seed_default_prompts=True``)
        succeeds — the seed composes with the outer ``BEGIN EXCLUSIVE``."""
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            run_lcm_migrations(conn, fts5_available=False)  # seed_default_prompts=True

            count = conn.execute("SELECT COUNT(*) FROM lcm_prompt_registry").fetchone()
            assert count[0] > 0
        finally:
            conn.close()

    def test_running_migration_twice_stays_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            run_lcm_migrations(conn, fts5_available=False)
            after1 = conn.execute("SELECT COUNT(*) FROM lcm_prompt_registry").fetchone()

            run_lcm_migrations(conn, fts5_available=False)
            after2 = conn.execute("SELECT COUNT(*) FROM lcm_prompt_registry").fetchone()

            assert after2[0] == after1[0]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Wave-9 P1: no forbidden placeholders
# ---------------------------------------------------------------------------


class TestNoForbiddenPlaceholders:
    """Wave-9 Agent #2/#7 P1 (2026-03-08).

    The renderer in :mod:`dispatch` (issue 07-05) does NOT substitute
    ``{{date_range}}`` or ``{{target_length}}`` — so if a seeded template
    contained either token, it would ship verbatim to the LLM. Same
    failure class as Final.review.3 Loop 4 Bug 4.2.

    Every seeded template MUST be free of both tokens. If a future
    edit introduces one, this guard fires.
    """

    def test_no_date_range_placeholder_in_any_seeded_template(self) -> None:
        for definition in DEFAULT_PROMPTS:
            assert "{{date_range}}" not in definition.template, (
                f"{definition.memory_type}/{definition.tier_label}/"
                f"{definition.pass_kind} contains forbidden "
                f"{{{{date_range}}}} placeholder (Wave-9 P1)"
            )

    def test_no_target_length_placeholder_in_any_seeded_template(self) -> None:
        for definition in DEFAULT_PROMPTS:
            assert "{{target_length}}" not in definition.template, (
                f"{definition.memory_type}/{definition.tier_label}/"
                f"{definition.pass_kind} contains forbidden "
                f"{{{{target_length}}}} placeholder (Wave-9 P1)"
            )


# ---------------------------------------------------------------------------
# SHA-256 byte-for-byte snapshot pins (vs TS source at commit 1f07fbd)
# ---------------------------------------------------------------------------


class TestSeedTemplateSnapshots:
    """SHA-256 snapshots of each seeded template.

    Each hash was computed from the TS source pinned to commit
    ``1f07fbd`` (branch ``pr-613``) via a Node helper that parses the
    template-literal text and runs :func:`createHash('sha256')` over it.
    The hashes therefore prove byte-for-byte parity between the TS and
    Python prompt strings.

    Drift in a Python template (intentional or accidental) surfaces as a
    test failure; reviewers cross-check against the TS source via
    ``lossless-claw/src/synthesis/seed-default-prompts.ts`` and update
    the hash deliberately, NOT quietly.

    Pattern mirrors :file:`tests/test_summarize_prompts.py` (PR #70) and
    :file:`tests/test_recall_policy.py` (PR #39).
    """

    EXPECTED_HASHES: dict[tuple[str, str | None, str], str] = {
        (
            "episodic-leaf",
            None,
            "single",
        ): "01ae237fbb04e636b64418bf5272a10d3780e16e25c9b672626b921884c71d00",
        (
            "episodic-condensed",
            "daily",
            "single",
        ): "954a4dc34ad8eb27b8154950ea9d2badb1b704c985d072aabf21df72f954f7c9",
        (
            "episodic-condensed",
            "weekly",
            "single",
        ): "8c9d5e23f1bb2ee5df35ceec6b7ecc71b82cec7d11791c409325b1bef8472e0a",
        (
            "episodic-condensed",
            "monthly",
            "single",
        ): "d17e417d097d92628bdee03ed038cf2e9557ce7e1b83c2906d492ec432b18d4d",
        (
            "episodic-condensed",
            "monthly",
            "verify_fidelity",
        ): "88597105f4ffaebb77b399a196bdd103556c947ead1cd0abd275fac0d0537914",
        (
            "episodic-yearly",
            "yearly",
            "single",
        ): "8d1ba29ad6868ef5f3acf173b3f88f9eb0957a3722546330370a040d59d4a347",
        (
            "episodic-yearly",
            "yearly",
            "best_of_n_judge",
        ): "5acdb06e20e3fd98a88c9af37da3354a454bb8a429cffd8f961c68a6e42f155f",
        (
            "episodic-condensed",
            "custom",
            "single",
        ): "59f60db206b36e695675e7df6e48fde5f005efd0fff52dae4fd8737e95205dd6",
        (
            "episodic-condensed",
            "filtered",
            "single",
        ): "a264baf93ce1f91c6893e2b3950091f9e30c06e3d300e38da5cbcfd5bc7a2d62",
        (
            "procedural-extract",
            None,
            "single",
        ): "8f455e1ec7354eb9559a87a3778ba03a74ce6fb69d1d4dcbebe378c7120b108a",
        (
            "entity-extract",
            None,
            "single",
        ): "1dac6d69934de4022ab63bdaf8a344e9e0a081d251deb281ee16e27147b28172",
    }

    def test_all_11_template_hashes_match_ts_pin(self) -> None:
        """Every seeded template hashes to its TS-source pin (commit ``1f07fbd``)."""
        for definition in DEFAULT_PROMPTS:
            triple = (definition.memory_type, definition.tier_label, definition.pass_kind)
            expected = self.EXPECTED_HASHES.get(triple)
            assert expected is not None, (
                f"unknown triple in DEFAULT_PROMPTS: {triple} — add to EXPECTED_HASHES"
            )
            actual = _sha256(definition.template)
            assert actual == expected, (
                f"template for {triple} drifted from TS pin (commit 1f07fbd). "
                f"actual={actual}, expected={expected}. If intentional, "
                f"recompute the TS hash and update EXPECTED_HASHES."
            )

    def test_expected_hashes_dict_size_matches_default_prompts(self) -> None:
        """The EXPECTED_HASHES dict has one entry per seeded prompt — no
        stale entries left over after a removal."""
        assert len(self.EXPECTED_HASHES) == len(DEFAULT_PROMPTS)


# ---------------------------------------------------------------------------
# Composes inside an outer transaction (BEGIN EXCLUSIVE shape)
# ---------------------------------------------------------------------------


class TestSeedComposesWithBeginExclusive:
    """The migration ladder wraps everything in ``BEGIN EXCLUSIVE``. The
    seed function uses raw ``INSERT`` (NOT :func:`register_prompt` which
    issues ``BEGIN IMMEDIATE``) so it does NOT open a nested transaction.

    Verify this by opening an outer ``BEGIN`` explicitly and running the
    seed inside it. A regression that used ``register_prompt`` for the
    seed would raise ``OperationalError: cannot start a transaction within
    a transaction`` here.
    """

    def test_seed_runs_inside_outer_begin(self, db_no_seed: sqlite3.Connection) -> None:
        db_no_seed.execute("BEGIN")
        try:
            result = seed_default_prompts(db_no_seed)
            db_no_seed.execute("COMMIT")
        except BaseException:  # pragma: no cover - defensive
            db_no_seed.execute("ROLLBACK")
            raise

        assert result.seeded > 0
        assert result.skipped == 0


# ---------------------------------------------------------------------------
# Seeded prompt_id format
# ---------------------------------------------------------------------------


class TestSeedPromptIdFormat:
    """Seeded prompt_ids match ``pr_<6 hex chars>`` (spec)."""

    def test_every_seeded_prompt_id_matches_format(self, db_no_seed: sqlite3.Connection) -> None:
        seed_default_prompts(db_no_seed)
        rows = db_no_seed.execute("SELECT prompt_id FROM lcm_prompt_registry").fetchall()
        assert len(rows) == len(DEFAULT_PROMPTS)
        for (prompt_id,) in rows:
            assert re.fullmatch(r"pr_[0-9a-f]{6}", prompt_id), (
                f"seeded prompt_id {prompt_id!r} does not match pr_<6 hex chars>"
            )
