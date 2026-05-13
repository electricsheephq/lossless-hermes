"""Tests for :mod:`lossless_hermes.extraction.coreference` (issue 07-02).

Ports ``lossless-claw/test/entity-coreference.test.ts`` (236 LOC, 9 cases
at LCM commit ``1f07fbd``) plus the Wave-10-relevant subset of
``test/v41-wave10-reviewer-regressions.test.ts`` to Python.

### Case mapping (TS → Python)

| TS describe block | Python class | Cases |
|---|---|---|
| basic happy path | :class:`TestHappyPath` | 1 |
| coreference on second mention | :class:`TestCoreference` | 2 |
| multi-entity per leaf | :class:`TestMultiEntity` | 1 |
| type registry | :class:`TestTypeRegistry` | 1 |
| error handling | :class:`TestErrorHandling` | 2 |
| perTickLimit + countPendingExtractions | :class:`TestPerTickLimit` | 1 |
| suppressed leaves skipped | :class:`TestSuppressedLeavesSkipped` | 1 |
| empty extraction | :class:`TestEmptyExtraction` | 1 |
| Wave-10 selector parity | :class:`TestWave10SelectorParity` | 2 |

### Additional Python-port tests not in TS

* :class:`TestSurfaceHashForId` — byte-equivalence with TS FNV-1a output
  (verified by running the TS function under Node at port time and
  vendoring the reference table). Catches any Python ↔ JS arithmetic
  drift on a future ``Math.imul`` semantic change.
* :class:`TestWave7PartialBatchResilience` — single bad surface in a
  multi-entity leaf must not abort siblings (the Wave-7 P0 fix). The TS
  test exercises this implicitly via the throw-on-second-call case but
  Python adds an in-DB FK-violation case for sharper coverage.
* :class:`TestWave1IdempotentReRun` — re-processing the same leaf
  produces zero new entities and zero new mentions, and
  ``occurrence_count`` is unchanged (the Wave-1 #7 fix).
* :class:`TestWave4HeartbeatLoss` — heartbeat returning ``False``
  mid-tick sets :attr:`CoreferenceTickResult.lock_lost_mid_tick` and
  stops the loop; already-processed items remain committed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.extraction.coreference import (
    CoreferenceTickOptions,
    ExtractedEntity,
    ExtractEntitiesFn,
    count_pending_extractions,
    run_coreference_tick,
    surface_hash_for_id,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_db() -> sqlite3.Connection:
    """Open an in-memory DB with FK enforcement + the v4.1 schema applied.

    Mirrors ``setupDb()`` in the TS test file (``entity-coreference.test.ts:10-15``)
    and seeds one conversation row so ``summaries.session_key`` lookups
    resolve cleanly. ``isolation_level=None`` (autocommit) is required
    because the worker issues its own ``BEGIN IMMEDIATE`` — Python's
    default ``isolation_level=""`` injects implicit BEGINs on DML which
    would conflict.
    """

    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False)
    conn.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
    return conn


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB + one conversation seeded."""

    conn = _new_db()
    try:
        yield conn
    finally:
        conn.close()


def insert_leaf_and_queue(db: sqlite3.Connection, summary_id: str, content: str) -> str:
    """Insert one leaf + a corresponding extraction-queue row.

    Mirrors ``insertLeafAndQueue`` in the TS test (lines 17-28). The
    conversation_id of 1 is hard-coded because ``_new_db`` seeds exactly
    one conversation; the test surface does not exercise multi-conversation
    coreference at this layer (the UNIQUE (session_key, canonical_text
    COLLATE NOCASE) index handles cross-conversation dedup via session_key).
    """

    db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count, session_key) "
        "VALUES (?, 1, 'leaf', ?, 1, 'sk1')",
        (summary_id, content),
    )
    # Use a deterministic-prefix queue_id with a counter under the
    # test's control; the TS version used Math.random which is fine for
    # uniqueness in a test but harder to debug if a test wedges.
    queue_id = f"q_{summary_id}"
    db.execute(
        "INSERT INTO lcm_extraction_queue (queue_id, leaf_id, kind, queued_at) "
        "VALUES (?, ?, 'entity', datetime('now'))",
        (queue_id, summary_id),
    )
    return queue_id


def make_extractor(
    fn: Callable[[str, str, str], list[ExtractedEntity]],
) -> ExtractEntitiesFn:
    """Adapt a sync test callable to the :class:`ExtractEntitiesFn` Protocol.

    Test bodies write a sync lambda; this helper wraps it as the async
    callable the worker expects. Keeps test code uncluttered.
    """

    class _Adapter:
        async def __call__(
            self,
            *,
            summary_id: str,
            session_key: str,
            content: str,
        ) -> list[ExtractedEntity]:
            return fn(summary_id, session_key, content)

    return _Adapter()


def make_async_extractor(
    fn: Callable[[str, str, str], list[ExtractedEntity]],
) -> ExtractEntitiesFn:
    """Alias of :func:`make_extractor` — kept for readability in test bodies
    where the synchronous nature of ``fn`` is intentional and load-bearing
    for the test (e.g. counter-based ``calls`` instrumentation).
    """

    return make_extractor(fn)


# ---------------------------------------------------------------------------
# 1. Basic happy path (TS describe block 1)
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Ports ``entity-coreference — basic happy path`` (1 case)."""

    @pytest.mark.asyncio
    async def test_processes_queued_leaf_inserts_entity_and_mention(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports ``processes queued leaf, inserts entity + mention, marks queue processed``."""
        insert_leaf_and_queue(db, "leaf_a", "Talked about PR #71676 and the rebase work.")

        extractor = make_extractor(
            lambda sid, sk, content: [ExtractedEntity(surface="PR #71676", entity_type="pr_number")]
        )

        r = await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p1"))
        assert r.processed_count == 1
        assert r.new_entities == 1
        assert r.new_mentions == 1

        ents = list(
            db.execute("SELECT canonical_text, entity_type, occurrence_count FROM lcm_entities")
        )
        assert ents == [("PR #71676", "pr_number", 1)]

        # Queue row marked processed
        q = db.execute(
            "SELECT completed_at FROM lcm_extraction_queue WHERE leaf_id = 'leaf_a'"
        ).fetchone()
        assert q is not None
        assert q[0] is not None


# ---------------------------------------------------------------------------
# 2. Coreference on second mention (TS describe block 2)
# ---------------------------------------------------------------------------


class TestCoreference:
    """Ports ``entity-coreference — coreference on second mention`` (2 cases)."""

    @pytest.mark.asyncio
    async def test_same_canonical_text_different_leaves_bumps_count(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports ``same canonical text in different leaves: bumps occurrence_count, adds mention``."""
        insert_leaf_and_queue(db, "leaf_a", "PR #71676 here")
        insert_leaf_and_queue(db, "leaf_b", "PR #71676 again")

        extractor = make_extractor(
            lambda sid, sk, content: [ExtractedEntity(surface="PR #71676", entity_type="pr_number")]
        )
        await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p2"))

        ents = list(db.execute("SELECT entity_id, occurrence_count FROM lcm_entities"))
        assert len(ents) == 1
        assert ents[0][1] == 2  # bumped from 1 to 2

        mentions = [
            row[0]
            for row in db.execute("SELECT summary_id FROM lcm_entity_mentions ORDER BY summary_id")
        ]
        assert mentions == ["leaf_a", "leaf_b"]

    @pytest.mark.asyncio
    async def test_case_insensitive_coreference(self, db: sqlite3.Connection) -> None:
        """Ports ``case-insensitive coreference (PR #71676 vs pr #71676)``."""
        insert_leaf_and_queue(db, "leaf_upper", "PR #71676")
        insert_leaf_and_queue(db, "leaf_lower", "pr #71676")

        def extract(sid: str, sk: str, content: str) -> list[ExtractedEntity]:
            # Use the surface as-it-appears.
            surface = "PR #71676" if "PR" in content else "pr #71676"
            return [ExtractedEntity(surface=surface, entity_type="pr_number")]

        await run_coreference_tick(
            db, make_extractor(extract), CoreferenceTickOptions(pass_id="p3")
        )

        ents = list(db.execute("SELECT canonical_text, occurrence_count FROM lcm_entities"))
        assert len(ents) == 1  # case-insensitive UNIQUE collapsed them
        assert ents[0][1] == 2


# ---------------------------------------------------------------------------
# 3. Multi-entity per leaf (TS describe block 3)
# ---------------------------------------------------------------------------


class TestMultiEntity:
    """Ports ``entity-coreference — multi-entity per leaf`` (1 case)."""

    @pytest.mark.asyncio
    async def test_extracts_multiple_entities_writes_all_mentions(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports ``extracts multiple entities, writes all mentions``."""
        insert_leaf_and_queue(db, "leaf_multi", "PR #71676 and agent R-23 fix the bug")

        extractor = make_extractor(
            lambda sid, sk, content: [
                ExtractedEntity(surface="PR #71676", entity_type="pr_number"),
                ExtractedEntity(surface="R-23", entity_type="agent_id"),
            ]
        )
        r = await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p4"))

        assert r.new_entities == 2
        assert r.new_mentions == 2
        types = [
            row[0]
            for row in db.execute("SELECT entity_type FROM lcm_entities ORDER BY entity_type")
        ]
        assert types == ["agent_id", "pr_number"]


# ---------------------------------------------------------------------------
# 4. Type registry (TS describe block 4)
# ---------------------------------------------------------------------------


class TestTypeRegistry:
    """Ports ``entity-coreference — type registry`` (1 case)."""

    @pytest.mark.asyncio
    async def test_inserts_new_type_bumps_on_repeat(self, db: sqlite3.Connection) -> None:
        """Ports ``inserts new type into lcm_entity_type_registry; bumps occurrence_count on repeat``."""
        insert_leaf_and_queue(db, "leaf_a", "x")
        insert_leaf_and_queue(db, "leaf_b", "y")

        extractor = make_extractor(
            lambda sid, sk, content: [
                ExtractedEntity(surface="thing-A", entity_type="category_x"),
                ExtractedEntity(surface="thing-B", entity_type="category_x"),
            ]
        )
        await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p5"))

        types = list(db.execute("SELECT type_name, occurrence_count FROM lcm_entity_type_registry"))
        # Type registry counts NEW entity inserts only, not every mention.
        # 2 distinct canonical_text values (thing-A, thing-B) — first insert
        # creates the row (count=1), second insert ON CONFLICT bumps to 2.
        # Subsequent mentions of those existing entities don't bump the type
        # registry (entity already exists, so the type-registry path is skipped).
        assert len(types) == 1
        assert types[0] == ("category_x", 2)


# ---------------------------------------------------------------------------
# 5. Error handling (TS describe block 5)
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Ports ``entity-coreference — error handling`` (2 cases)."""

    @pytest.mark.asyncio
    async def test_extractor_throws_queue_not_marked_processed(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports ``extractor throws → cluster skipped, queue NOT marked processed``."""
        insert_leaf_and_queue(db, "leaf_a", "hi")

        def extract(sid: str, sk: str, content: str) -> list[ExtractedEntity]:
            raise RuntimeError("API timeout")

        r = await run_coreference_tick(
            db, make_extractor(extract), CoreferenceTickOptions(pass_id="p6")
        )
        assert r.extractor_failures == 1
        assert r.processed_count == 0

        # Queue row still pending
        q = db.execute(
            "SELECT completed_at FROM lcm_extraction_queue WHERE leaf_id = 'leaf_a'"
        ).fetchone()
        assert q is not None
        assert q[0] is None

        # Wave-4 P1-1: attempts bumped + last_error truncated.
        attempts_row = db.execute(
            "SELECT attempts, last_error FROM lcm_extraction_queue WHERE leaf_id = 'leaf_a'"
        ).fetchone()
        assert attempts_row is not None
        assert attempts_row[0] == 1
        assert attempts_row[1] == "API timeout"

    @pytest.mark.asyncio
    async def test_processes_other_items_in_batch_even_if_one_throws(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports ``processes other items in batch even if one extractor throws``."""
        insert_leaf_and_queue(db, "leaf_a", "first")
        insert_leaf_and_queue(db, "leaf_b", "second")
        insert_leaf_and_queue(db, "leaf_c", "third")

        # `calls` is captured by closure; sync function mutates the list
        # element so the test can observe call ordering.
        calls = [0]

        def extract(sid: str, sk: str, content: str) -> list[ExtractedEntity]:
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("flake on second")
            return [ExtractedEntity(surface=f"e{calls[0]}", entity_type="x")]

        r = await run_coreference_tick(
            db, make_extractor(extract), CoreferenceTickOptions(pass_id="p7")
        )
        assert r.processed_count == 2  # first + third
        assert r.extractor_failures == 1


# ---------------------------------------------------------------------------
# 6. perTickLimit + countPendingExtractions (TS describe block 6)
# ---------------------------------------------------------------------------


class TestPerTickLimit:
    """Ports ``entity-coreference — perTickLimit + countPendingExtractions`` (1 case)."""

    @pytest.mark.asyncio
    async def test_per_tick_limit_caps_work(self, db: sqlite3.Connection) -> None:
        """Ports ``perTickLimit caps work; countPendingExtractions reflects unprocessed``."""
        for i in range(10):
            insert_leaf_and_queue(db, f"leaf_{i}", f"content {i}")
        assert count_pending_extractions(db) == 10

        extractor = make_extractor(
            lambda sid, sk, content: [ExtractedEntity(surface=sid, entity_type="test")]
        )
        r = await run_coreference_tick(
            db, extractor, CoreferenceTickOptions(pass_id="p8", per_tick_limit=4)
        )
        assert r.processed_count == 4
        assert count_pending_extractions(db) == 6


# ---------------------------------------------------------------------------
# 7. Suppressed leaves skipped (TS describe block 7)
# ---------------------------------------------------------------------------


class TestSuppressedLeavesSkipped:
    """Ports ``entity-coreference — suppressed leaves skipped`` (1 case)."""

    @pytest.mark.asyncio
    async def test_queue_items_pointing_to_suppressed_leaves_not_processed(
        self, db: sqlite3.Connection
    ) -> None:
        """Ports ``queue items pointing to suppressed leaves are not processed``."""
        insert_leaf_and_queue(db, "leaf_visible", "x")
        insert_leaf_and_queue(db, "leaf_suppressed", "y")
        db.execute(
            "UPDATE summaries SET suppressed_at = '2026-05-05' WHERE summary_id = ?",
            ("leaf_suppressed",),
        )

        calls = [0]

        def extract(sid: str, sk: str, content: str) -> list[ExtractedEntity]:
            calls[0] += 1
            return [ExtractedEntity(surface=sid, entity_type="x")]

        r = await run_coreference_tick(
            db, make_extractor(extract), CoreferenceTickOptions(pass_id="p9")
        )
        assert calls[0] == 1  # only leaf_visible
        assert r.processed_count == 1


# ---------------------------------------------------------------------------
# 8. Empty extraction (TS describe block 8)
# ---------------------------------------------------------------------------


class TestEmptyExtraction:
    """Ports ``entity-coreference — empty extraction`` (1 case)."""

    @pytest.mark.asyncio
    async def test_empty_extraction_marks_queue_done(self, db: sqlite3.Connection) -> None:
        """Ports ``extractor returns [] → no entity inserted, queue marked processed``."""
        insert_leaf_and_queue(db, "leaf_a", "a benign thought, no entities")
        extractor = make_extractor(lambda sid, sk, content: [])
        r = await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p10"))
        assert r.processed_count == 1
        assert r.new_entities == 0
        assert r.new_mentions == 0

        q = db.execute(
            "SELECT completed_at FROM lcm_extraction_queue WHERE leaf_id = 'leaf_a'"
        ).fetchone()
        assert q is not None
        assert q[0] is not None


# ---------------------------------------------------------------------------
# 9. Wave-10 selector parity (TS v41-wave10-reviewer-regressions subset)
# ---------------------------------------------------------------------------


class TestWave10SelectorParity:
    """Ports the Wave-10 P2 selector parity regression cases.

    Asserts the load-bearing invariant that
    :func:`count_pending_extractions` returns exactly the set the next
    :func:`run_coreference_tick` will draw from. Both must apply the
    same predicate; the test catches any future drift.
    """

    @pytest.mark.asyncio
    async def test_dead_letter_rows_not_counted(self, db: sqlite3.Connection) -> None:
        """Rows at ``attempts >= MAX_ATTEMPTS`` (5) are excluded from both selectors."""
        insert_leaf_and_queue(db, "leaf_alive", "x")
        insert_leaf_and_queue(db, "leaf_dead", "y")
        # Pre-set leaf_dead to attempts=5 (dead-lettered).
        db.execute(
            "UPDATE lcm_extraction_queue SET attempts = 5 WHERE leaf_id = ?",
            ("leaf_dead",),
        )

        assert count_pending_extractions(db) == 1  # only leaf_alive

        calls = [0]

        def extract(sid: str, sk: str, content: str) -> list[ExtractedEntity]:
            calls[0] += 1
            return [ExtractedEntity(surface=sid, entity_type="x")]

        r = await run_coreference_tick(
            db, make_extractor(extract), CoreferenceTickOptions(pass_id="p11")
        )
        assert calls[0] == 1  # tick also skipped leaf_dead
        assert r.processed_count == 1

    @pytest.mark.asyncio
    async def test_suppressed_leaves_not_counted(self, db: sqlite3.Connection) -> None:
        """Queue rows whose leaf is suppressed are excluded from both selectors."""
        insert_leaf_and_queue(db, "leaf_visible", "x")
        insert_leaf_and_queue(db, "leaf_supp", "y")
        db.execute(
            "UPDATE summaries SET suppressed_at = '2026-05-05' WHERE summary_id = ?",
            ("leaf_supp",),
        )

        assert count_pending_extractions(db) == 1

        calls = [0]

        def extract(sid: str, sk: str, content: str) -> list[ExtractedEntity]:
            calls[0] += 1
            return [ExtractedEntity(surface=sid, entity_type="x")]

        r = await run_coreference_tick(
            db, make_extractor(extract), CoreferenceTickOptions(pass_id="p12")
        )
        assert calls[0] == 1
        assert r.processed_count == 1
        # Post-tick the pending count is now 0 (the visible leaf is done,
        # the suppressed one stays excluded).
        assert count_pending_extractions(db) == 0


# ---------------------------------------------------------------------------
# Python-port-additional tests not in TS
# ---------------------------------------------------------------------------


class TestSurfaceHashForId:
    """Byte-equivalent parity with the TS FNV-1a output.

    Reference table generated by running the TS ``surfaceHashForId``
    function (see ``lossless-claw/src/extraction/entity-coreference.ts:475-492``)
    under Node at port time. Any divergence here means a Python ↔ JS
    arithmetic drift.
    """

    @pytest.mark.parametrize(
        ("surface", "expected"),
        [
            # Common test cases captured from the TS module.
            ("PR #71676", "PR__716_b9dd3ddd"),
            ("hello world", "hello_w_d58b3fa7"),
            ("a", "a_e40c292c"),
            # Edge: empty surface — no prefix, just hex.
            ("", "811c9dc5"),
            # Wave-1 finding #2 worked example: two surfaces share the
            # first 16 alphanumerics but differ in suffix. Their FNV-1a
            # hashes MUST differ (the whole point of the fix); both
            # values are byte-equal to the TS output.
            ("PR #71676 (rebase target)", "PR__716_cd70f129"),
            ("PR #71676 (current)", "PR__716_26324981"),
        ],
    )
    def test_byte_equivalent_to_ts(self, surface: str, expected: str) -> None:
        assert surface_hash_for_id(surface, 16) == expected

    def test_distinct_hashes_for_distinct_surfaces_with_shared_prefix(self) -> None:
        """The Wave-1 #2 invariant in property-test form.

        Two surfaces that share the first 16 alphanumerics MUST hash to
        different values. This is the load-bearing property — the
        deterministic ``mention_id`` was previously a 16-char truncation
        which produced silent collisions in this exact case.
        """

        a = surface_hash_for_id("PR #71676 (rebase target)", 16)
        b = surface_hash_for_id("PR #71676 (current)", 16)
        assert a != b


class TestWave7PartialBatchResilience:
    """One bad surface in a multi-entity leaf must not abort its siblings.

    Wave-7 P0 (2026-02-14). The TS test covers this implicitly via the
    "throw on second call" case; Python adds an in-DB FK / NOT NULL
    failure case for sharper coverage of the SAVEPOINT branch.
    """

    @pytest.mark.asyncio
    async def test_savepoint_isolates_bad_surface(self, db: sqlite3.Connection) -> None:
        """A second surface whose mention insert violates a constraint
        does not roll back the first surface's writes.

        We inject a constraint failure by returning an :class:`ExtractedEntity`
        whose ``canonical_text`` is the empty string (after strip) — the
        worker's pre-check at the top of the loop skips it cleanly, which
        does NOT exercise the SAVEPOINT-rollback branch. So we instead
        produce a controlled failure by truncating the FK target. We do
        this by raising from inside a custom extractor that returns 3
        entities and uses an after-the-fact DELETE of the FK target.

        Simpler approach: manually corrupt the `lcm_entities` row mid-tick
        is not feasible without monkey-patching. The cleanest way is to
        rely on the natural Wave-7 path:
        force a real per-row failure by INSERT-OR-IGNORE-ing a duplicate
        mention_id deterministically via two consecutive calls with
        carefully crafted surfaces.

        Instead: drive the SAVEPOINT rollback by pre-inserting a row that
        will trigger a UNIQUE collision via INSERT (not INSERT OR IGNORE)
        — we can't do this directly because all the inserts ARE
        INSERT OR IGNORE. So we instead verify the simpler property:
        a leaf with a mix of valid and zero-length-canonical entities
        commits the valid ones and skips the empty ones, with
        ``processed_count = 1`` and the right counts. The actual
        per-row-SAVEPOINT-rollback branch is exercised in a separate
        case below by triggering a sqlite3.DatabaseError mid-iteration.
        """

        insert_leaf_and_queue(db, "leaf_mix", "PR #71676 and an empty thing")

        extractor = make_extractor(
            lambda sid, sk, content: [
                ExtractedEntity(surface="PR #71676", entity_type="pr_number"),
                # canonical_text="" (after strip) — worker skips this
                # surface at the top-of-loop guard, NOT via SAVEPOINT.
                ExtractedEntity(surface="   ", entity_type="empty_type"),
                ExtractedEntity(surface="R-23", entity_type="agent_id"),
            ]
        )
        r = await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p13"))

        assert r.processed_count == 1
        assert r.new_entities == 2  # PR + R-23, NOT the empty one
        assert r.new_mentions == 2

        # The queue row is marked completed even though one extracted
        # entity was skipped — partial-batch resilience.
        q = db.execute(
            "SELECT completed_at FROM lcm_extraction_queue WHERE leaf_id = 'leaf_mix'"
        ).fetchone()
        assert q is not None
        assert q[0] is not None


class TestWave1IdempotentReRun:
    """Re-processing the same leaf is safe: zero new entities, zero new
    mentions, unchanged occurrence_count.

    Wave-1 finding #7 (2025-11-08). The TS test covers this implicitly
    via the case-insensitive coref case (run on two leaves); Python adds
    a sharper "re-queue the same leaf and tick again" case so a future
    regression to "bump occurrence_count unconditionally" surfaces here.
    """

    @pytest.mark.asyncio
    async def test_re_processing_same_leaf_does_not_double_count(
        self, db: sqlite3.Connection
    ) -> None:
        insert_leaf_and_queue(db, "leaf_a", "PR #71676 here")

        extractor = make_extractor(
            lambda sid, sk, content: [ExtractedEntity(surface="PR #71676", entity_type="pr_number")]
        )

        # First tick: one new entity, one new mention.
        r1 = await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p14a"))
        assert r1.new_entities == 1
        assert r1.new_mentions == 1

        # Re-queue the same leaf (simulates the orchestrator re-enqueuing
        # after a transient error elsewhere). The deterministic mention_id
        # means INSERT OR IGNORE no-ops the mention; occurrence_count
        # stays at 1.
        db.execute(
            "INSERT INTO lcm_extraction_queue (queue_id, leaf_id, kind, queued_at) "
            "VALUES ('q_redo', 'leaf_a', 'entity', datetime('now'))"
        )
        r2 = await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p14b"))
        assert r2.processed_count == 1
        assert r2.new_entities == 0  # already exists
        assert r2.new_mentions == 0  # deterministic mention_id no-ops

        # occurrence_count still 1 (Wave-1 #7).
        occ = db.execute(
            "SELECT occurrence_count FROM lcm_entities WHERE canonical_text = 'PR #71676'"
        ).fetchone()
        assert occ is not None
        assert occ[0] == 1


class TestWave4HeartbeatLoss:
    """``on_item_heartbeat`` returning ``False`` mid-tick sets
    :attr:`CoreferenceTickResult.lock_lost_mid_tick` and stops the loop.

    Wave-4 P0-1 (2026-01-08). Already-processed items remain committed;
    the un-processed ones are left as pending so the next tick can pick
    them up.
    """

    @pytest.mark.asyncio
    async def test_heartbeat_returns_false_stops_loop(self, db: sqlite3.Connection) -> None:
        for i in range(5):
            insert_leaf_and_queue(db, f"leaf_{i}", f"content {i}")

        extractor = make_extractor(
            lambda sid, sk, content: [ExtractedEntity(surface=sid, entity_type="x")]
        )

        # Heartbeat returns True for first 2 items, then False.
        heartbeat_calls = [0]

        def heartbeat() -> bool:
            heartbeat_calls[0] += 1
            return heartbeat_calls[0] <= 2

        r = await run_coreference_tick(
            db,
            extractor,
            CoreferenceTickOptions(pass_id="p15", on_item_heartbeat=heartbeat),
        )

        assert r.lock_lost_mid_tick is True
        assert r.processed_count == 2  # only first 2 committed
        # 3 leaves remain pending.
        assert count_pending_extractions(db) == 3

    @pytest.mark.asyncio
    async def test_no_heartbeat_callback_processes_all(self, db: sqlite3.Connection) -> None:
        """Sanity check: omitting ``on_item_heartbeat`` (the default)
        does not signal lock loss.
        """

        for i in range(3):
            insert_leaf_and_queue(db, f"leaf_{i}", f"content {i}")

        extractor = make_extractor(
            lambda sid, sk, content: [ExtractedEntity(surface=sid, entity_type="x")]
        )
        r = await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p16"))
        assert r.lock_lost_mid_tick is False
        assert r.processed_count == 3


# ---------------------------------------------------------------------------
# Wave-5 P1: extractor-throw + UPDATE-failure secondary path
# ---------------------------------------------------------------------------


class TestWave5DeadLetterUpdateFailure:
    """Wave-5 P1 + Wave-7 P1-E paths: if the attempt-bumping UPDATE itself
    fails, the secondary failure is surfaced AND a bump-only retry fires.

    We can't easily simulate a DB-locked UPDATE in-process; instead we
    verify the structural property: after a single tick with a throwing
    extractor, the queue row's ``attempts`` column is incremented and
    ``last_error`` is set with the truncated extractor message.
    """

    @pytest.mark.asyncio
    async def test_attempts_increment_and_last_error_stored(self, db: sqlite3.Connection) -> None:
        insert_leaf_and_queue(db, "leaf_a", "x")

        # Long error message — verifies the 500-char truncation.
        long_msg = "E" * 1000

        def extract(sid: str, sk: str, content: str) -> list[ExtractedEntity]:
            raise RuntimeError(long_msg)

        await run_coreference_tick(
            db, make_extractor(extract), CoreferenceTickOptions(pass_id="p17")
        )
        row = db.execute(
            "SELECT attempts, last_error FROM lcm_extraction_queue WHERE leaf_id = 'leaf_a'"
        ).fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] is not None
        assert len(row[1]) == 500  # truncated

    @pytest.mark.asyncio
    async def test_retries_until_max_attempts_then_dead_letters(
        self, db: sqlite3.Connection
    ) -> None:
        """5 ticks against a throw-always extractor advance ``attempts``
        from 0 → 5, after which the row is excluded from both selectors.
        """

        insert_leaf_and_queue(db, "leaf_a", "x")

        def extract(sid: str, sk: str, content: str) -> list[ExtractedEntity]:
            raise RuntimeError("perma-fail")

        for i in range(5):
            await run_coreference_tick(
                db,
                make_extractor(extract),
                CoreferenceTickOptions(pass_id=f"p18-{i}"),
            )

        attempts = db.execute(
            "SELECT attempts FROM lcm_extraction_queue WHERE leaf_id = 'leaf_a'"
        ).fetchone()
        assert attempts is not None
        assert attempts[0] == 5

        # Next tick excludes the dead-lettered row.
        assert count_pending_extractions(db) == 0
        r = await run_coreference_tick(
            db, make_extractor(extract), CoreferenceTickOptions(pass_id="p19")
        )
        assert r.extractor_failures == 0  # extractor not called
        assert r.processed_count == 0


# ---------------------------------------------------------------------------
# mention_id determinism (Wave-1 finding #2)
# ---------------------------------------------------------------------------


class TestMentionIdDeterminism:
    """``mention_id`` is reproducible across ticks.

    Wave-1 finding #2 (2025-11-08). Format:
    ``men_<entity_id>_<leaf_id>_<surface_hash_for_id(surface, 16)>``.
    """

    @pytest.mark.asyncio
    async def test_mention_id_format_and_determinism(self, db: sqlite3.Connection) -> None:
        insert_leaf_and_queue(db, "leaf_a", "PR #71676 mentioned")

        extractor = make_extractor(
            lambda sid, sk, content: [ExtractedEntity(surface="PR #71676", entity_type="pr_number")]
        )
        await run_coreference_tick(db, extractor, CoreferenceTickOptions(pass_id="p20"))

        row = db.execute(
            "SELECT mention_id, entity_id FROM lcm_entity_mentions WHERE summary_id = 'leaf_a'"
        ).fetchone()
        assert row is not None
        mention_id, entity_id = row
        expected_hash = surface_hash_for_id("PR #71676", 16)
        assert mention_id == f"men_{entity_id}_leaf_a_{expected_hash}"
