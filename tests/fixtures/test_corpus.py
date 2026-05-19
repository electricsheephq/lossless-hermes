"""Synthetic LCM v4.1 test corpus — Python port of ``v41-test-corpus.ts``.

Ports ``lossless-claw/test/fixtures/v41-test-corpus.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``). The TS fixture is the durable,
checked-in, deterministic conversation corpus that LCM's end-to-end
tests run against; this is its faithful Python equivalent.

### Why this exists

Issue 09-08 (epic 09 — eval) names ``v41-test-corpus`` as its own
Path-B prerequisite: the +52.5pp Voyage recall benchmark must run
against a fixed corpus, and Eva's real ``~/.openclaw/lcm.db`` snapshot
(≈2.6 GB, private) is unavailable. This module is the reproducible
corpus the benchmark queries — it needs no API, seeds deterministically,
and carries zero PII.

The :data:`tests.fixtures.eva_baseline_v2.CORPUS_SUMMARY_IDS` frozenset
is the ground-truth ID universe; every ``summary_id`` seeded here MUST
appear in that set (and vice versa) — the
``test_seeded_ids_match_corpus_summary_ids_bidirectionally`` test in
``tests/fixtures/test_test_corpus.py`` enforces that bidirectional
parity, so a future drift in either file fails fast.

### What's in it

* **5 conversations** across 4 ``session_key`` families (a rolled-over
  main thread, a customer thread, a legacy thread, a sub-agent thread).
* **54 leaf summaries** — verbatim known-content leaves (Type C1-C5),
  topic leaves (Type B1-B4), entity leaves (Type D2/D4), the +52.5pp
  lineage leaf (Type E1), recency leaves (Type A1/A3), CJK regression
  leaves, 2 suppressed leaves, a legacy leaf, and the Wave-10 adversarial
  fixture leaves. (The TS module-header comment loosely says "~80
  leaves"; the shipped ``FIXTURE_LEAVES`` array is 54 — this port
  matches the shipped array, which is what ``CORPUS_SUMMARY_IDS`` in
  ``eva_baseline_v2.py`` was authored against.)
* **2 condensed summaries** with parent/child links (Type E drilldown).
* **4 entities** with known mention counts (Type D2 + D4).
* **54 messages** — one ``user`` message backing each leaf (verbatim
  search queries the messages table directly).

### Determinism

All timestamps are computed relative to a fixed :data:`BASE_DATE`
(2026-05-07T12:00:00Z) so the fixture's "yesterday" is May 6.
:func:`build_test_corpus` is pure — re-running it produces byte-identical
rows (modulo the entity-table ``first_seen_at`` / ``last_seen_at`` and
suppressed-message ``suppressed_at``, which the TS source itself derives
from ``datetime('now')`` — see the inline note on
:func:`build_test_corpus`).

### Port fidelity notes

* The TS source mutates the corpus in-place via ``DatabaseSync`` prepared
  statements; this port mirrors the exact INSERT statements + column
  order, so the resulting rows are schema-identical.
* The TS ``buildTestCorpus`` runs ``runLcmMigrations`` itself. This port
  does the same via :func:`run_lcm_migrations` so a caller can pass a
  bare connection.
* ``messages_fts`` is rowid-indexed (``fts5(content, ...)``);
  ``summaries_fts`` is ``fts5(summary_id UNINDEXED, content, ...)``. The
  Wave-10 sub-agent #3 fix in the TS source (insert ``summaries_fts`` via
  ``(summary_id, content)``, NOT ``(rowid, content)``) is preserved here
  verbatim — see the inline Wave-10 comment.
* Suppressed leaves get neither a ``summaries_fts`` row nor a
  ``messages_fts`` row, matching the TS source's parity with
  production-side FTS triggers (which exclude suppressed content).

See:

* ``epics/09-eval/09-08-benchmarks.md`` — the issue this corpus unblocks.
* ``lossless-claw/test/fixtures/v41-test-corpus.ts`` — the TS source.
* ``tests/fixtures/eva_baseline_v2.py`` — the query set whose
  ``expected_summary_ids`` resolve against this corpus.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from lossless_hermes.db.migration import run_lcm_migrations

__all__ = [
    "BASE_DATE",
    "FIXTURE_CONDENSED",
    "FIXTURE_CONVERSATIONS",
    "FIXTURE_ENTITIES",
    "FIXTURE_LEAVES",
    "CorpusMetadata",
    "FixtureCondensed",
    "FixtureConversation",
    "FixtureEntity",
    "FixtureLeaf",
    "build_test_corpus",
]


# ---------------------------------------------------------------------------
# Anchor date + time helpers (ports v41-test-corpus.ts:60-77)
# ---------------------------------------------------------------------------

BASE_DATE = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
"""Anchor date for the entire fixture (``v41-test-corpus.ts:67``).

A fixed UTC timestamp so "yesterday" / "last week" / "this month" are all
stable across runs. Tests that compare against "now" should compute their
expected values relative to this constant, not ``datetime.now()``.
"""

_MINUTE = 60 * 1000
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR


def _time_ago(ms: int) -> str:
    """Subtract ``ms`` milliseconds from :data:`BASE_DATE`; return ISO string.

    Ports TS ``timeAgo`` (``v41-test-corpus.ts:75-77``). The TS source
    calls ``new Date(...).toISOString()`` which yields a millisecond-
    precision ``...Z`` string (e.g. ``2026-05-01T12:00:00.000Z``); this
    reproduces that exact shape so the seeded ``created_at`` values are
    byte-identical to the TS fixture.
    """
    dt = BASE_DATE - timedelta(milliseconds=ms)
    # JS Date#toISOString() always renders millisecond precision + 'Z'.
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Fixture row dataclasses (port the TS interfaces)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FixtureConversation:
    """A ``conversations`` fixture row. Ports ``FIXTURE_CONVERSATIONS``."""

    conversation_id: int
    session_id: str
    session_key: str
    active: int
    created_at: str


@dataclass(frozen=True, slots=True)
class FixtureLeaf:
    """A leaf-summary fixture row. Ports the TS ``FixtureLeaf`` interface.

    Attributes:
        summary_id: Unique leaf id, keyed by which scenario it supports.
        conversation_id: FK to :class:`FixtureConversation`.
        session_key: The leaf's session scope.
        content: What searches match against — carries the literal
            phrases tests assert on.
        token_count: Synthetic token count.
        aged_hours: Hours-ago from :data:`BASE_DATE` for ``created_at``.
        suppressed: When ``True`` the leaf (and its backing message) are
            suppressed — excluded from every agent-facing read path.
        tags: Which test scenario(s) this leaf supports.
    """

    summary_id: str
    conversation_id: int
    session_key: str
    content: str
    token_count: int
    aged_hours: float
    suppressed: bool = False
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FixtureCondensed:
    """A condensed-summary fixture row. Ports the TS ``FixtureCondensed``."""

    summary_id: str
    conversation_id: int
    session_key: str
    content: str
    token_count: int
    aged_hours: float
    child_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FixtureEntity:
    """An entity fixture row + its mentions. Ports the TS ``FixtureEntity``."""

    entity_id: str
    session_key: str
    canonical_text: str
    entity_type: str
    occurrence_count: int
    mentioned_in: tuple[str, ...] = field(default_factory=tuple)


class CorpusMetadata(TypedDict):
    """Return shape of :func:`build_test_corpus`.

    Ports the TS ``buildTestCorpus`` return object — a metadata dict tests
    use to know what was inserted.
    """

    base_date: datetime
    conversations: tuple[FixtureConversation, ...]
    leaf_count: int
    condensed_count: int
    entity_count: int
    suppressed_count: int
    leaf_summary_ids: tuple[str, ...]
    condensed_summary_ids: tuple[str, ...]


# ---------------------------------------------------------------------------
# Conversation rows (ports v41-test-corpus.ts:82-118)
# ---------------------------------------------------------------------------

FIXTURE_CONVERSATIONS: tuple[FixtureConversation, ...] = (
    FixtureConversation(
        conversation_id=1,
        session_id="fixture-conv-001",
        session_key="agent:main:main",
        active=0,  # older rolled-over main thread
        created_at=_time_ago(30 * _DAY),
    ),
    FixtureConversation(
        conversation_id=2,
        session_id="fixture-conv-002",
        session_key="agent:main:main",
        active=1,  # current active main thread
        created_at=_time_ago(7 * _DAY),
    ),
    FixtureConversation(
        conversation_id=3,
        session_id="fixture-conv-003",
        session_key="agent:operator-vm:main",
        active=1,  # operator-VM customer thread (Type D2 + C3)
        created_at=_time_ago(14 * _DAY),
    ),
    FixtureConversation(
        conversation_id=4,
        session_id="fixture-conv-004",
        session_key="legacy:conv_503",
        active=0,  # legacy thread (tests legacy: prefix scoping)
        created_at=_time_ago(60 * _DAY),
    ),
    FixtureConversation(
        conversation_id=5,
        session_id="fixture-conv-005",
        session_key="agent:main:subagent:harness",
        active=1,  # sub-agent thread (Type E delegation)
        created_at=_time_ago(2 * _DAY),
    ),
)


def _d2_leaves() -> list[FixtureLeaf]:
    """Type D2 — operator-VM customer entity leaves (5).

    Ports the ``Array.from({ length: 5 }, ...)`` block at
    ``v41-test-corpus.ts:248-256``.
    """
    return [
        FixtureLeaf(
            summary_id=f"sum_d2_{i + 1:03d}",
            conversation_id=3,
            session_key="agent:operator-vm:main",
            content=(
                f"Operator-VM customer follow-up #{i + 1}: customer reported "
                f"gateway timeout 30s; investigated CPU spike correlation with "
                f"smarter-claw plugin path; recommended operator disable "
                f"smarter-claw step in plan mode workflow."
            ),
            token_count=55,
            aged_hours=(12 + i) * 24,
            tags=("D2",),
        )
        for i in range(5)
    ]


def _d4_leaves() -> list[FixtureLeaf]:
    """Type D4 — Voyage entity leaves (6).

    Ports the ``Array.from({ length: 6 }, ...)`` block at
    ``v41-test-corpus.ts:258-266``.
    """
    return [
        FixtureLeaf(
            summary_id=f"sum_d4_{i + 1:03d}",
            conversation_id=2,
            session_key="agent:main:main",
            content=(
                f"Voyage discussion #{i + 1}: voyage-4-large embedding model + "
                f"rerank-2.5. Voyage API key resolution. Voyage retry policy. "
                f"Voyage token budget 80K per batch."
            ),
            token_count=50,
            aged_hours=(1 + i * 2) * 24,
            tags=("D4", "B4"),
        )
        for i in range(6)
    ]


def _a1_leaves() -> list[FixtureLeaf]:
    """Type A1 — yesterday's work items (6).

    Ports the ``Array.from({ length: 6 }, ...)`` block at
    ``v41-test-corpus.ts:279-287``.
    """
    return [
        FixtureLeaf(
            summary_id=f"sum_a1_{i + 1:03d}",
            conversation_id=2,
            session_key="agent:main:main",
            content=(
                f"Yesterday's work item #{i + 1}: completed Wave-9 audit fix "
                f"#{i + 1}. Includes test coverage for the regression. "
                f"Status: shipped."
            ),
            token_count=35,
            aged_hours=24 + i * 2,  # ~24-36 hours ago
            tags=("A1",),
        )
        for i in range(6)
    ]


def _a3_leaves() -> list[FixtureLeaf]:
    """Type A3 — week of April 26-May 2 recap items (8).

    Ports the ``Array.from({ length: 8 }, ...)`` block at
    ``v41-test-corpus.ts:289-297``.
    """
    return [
        FixtureLeaf(
            summary_id=f"sum_a3_{i + 1:03d}",
            conversation_id=1,
            session_key="agent:main:main",
            content=(
                f"Week recap item #{i + 1}: rebase work on PR #71676, "
                f"race-fix testing, gateway restart cycle."
            ),
            token_count=30,
            aged_hours=(8 + i) * 24,  # 8-15 days ago
            tags=("A3",),
        )
        for i in range(8)
    ]


# ---------------------------------------------------------------------------
# Leaf summary rows (ports v41-test-corpus.ts:147-569)
# ---------------------------------------------------------------------------

FIXTURE_LEAVES: tuple[FixtureLeaf, ...] = (
    # ── Type C1: Verbatim Eva quote about lcm_recent rejection ──
    FixtureLeaf(
        summary_id="sum_c1_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Eva said: I want to throw out rollups; they're worse than "
            "condensed summaries. lcm_recent is the only thing in the way. "
            "Replacing it with synthesize_around period mode."
        ),
        token_count=50,
        aged_hours=6 * 24,  # 6 days ago — within "last week"
        tags=("C1", "B5"),
    ),
    # ── Type C2: Verbatim decision wording ──
    FixtureLeaf(
        summary_id="sum_c2_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Decision recorded: throw out rollups in favor of condensed "
            "summaries + period mode synthesize_around. Approved by Eva. "
            "PR #613 ships this."
        ),
        token_count=40,
        aged_hours=5 * 24,
        tags=("C2", "B5"),
    ),
    # ── Type C3: Operator-VM customer escalation ──
    FixtureLeaf(
        summary_id="sum_c3_001",
        conversation_id=3,
        session_key="agent:operator-vm:main",
        content=(
            "Eva's exact words from the operator-VM customer escalation: "
            "'Customer reported gateway timeout 30s on first plugin install. "
            "They saw a CPU spike and the worker process never came back.'"
        ),
        token_count=55,
        aged_hours=10 * 24,
        tags=("C3", "D2"),
    ),
    # ── Type C4: Verbatim error message from backfill autostart ──
    FixtureLeaf(
        summary_id="sum_c4_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "[lcm] backfill autostart: VOYAGE_API_KEY not set; cannot start "
            "backfill worker. autostart returning NO_OP_HANDLE; existing "
            "leaves remain unembedded until operator sets the key."
        ),
        token_count=45,
        aged_hours=3 * 24,
        tags=("C4",),
    ),
    # ── Type C5: Verbatim commit message ──
    FixtureLeaf(
        summary_id="sum_c5_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Commit 1081067476: 'fix: persist plan_steps + title "
            "synchronously to eliminate empty-plan-body race'. Extends "
            "persistPlanApprovalRequest in "
            "pi-embedded-subscribe.handlers.tools.ts to write lastPlanSteps "
            "+ title synchronously, eliminating the race with the async "
            "plan-snapshot-persister."
        ),
        token_count=75,
        aged_hours=12 * 24,
        tags=("C5", "A4"),
    ),
    # ── Type B1: worker_threads heartbeat isolation ──
    FixtureLeaf(
        summary_id="sum_b1_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Discussion of worker_threads heartbeat isolation as a future "
            "enhancement: cycle-3 task to isolate the heartbeat thread from "
            "the main worker so a stuck LLM call cannot block the lock TTL "
            "extension. Currently single-process, so deferred."
        ),
        token_count=60,
        aged_hours=4 * 24,
        tags=("B1",),
    ),
    # ── Type B2: hybrid search rerank ──
    FixtureLeaf(
        summary_id="sum_b2_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Voyage rerank-2.5 in hybrid_search lifts paraphrastic recall by "
            "+52.5pp over FTS-only. Rerank token budget is 600K but "
            "currently not enforced; large queries silently degrade to RRF "
            "fusion fallback. Worth tracking."
        ),
        token_count=55,
        aged_hours=8 * 24,
        tags=("B2", "E1"),
    ),
    # ── Type B3: race condition lineage ──
    FixtureLeaf(
        summary_id="sum_b3_001",
        conversation_id=1,
        session_key="agent:main:main",
        content=(
            "Hit a race condition where empty-plan-body slipped through "
            "because lastPlanSteps was written async by the snapshot "
            "persister. Same class as the v4.1 reconcileSessionKeys TOCTOU "
            "race that Wave-8 fixed by moving snapshot inside BEGIN "
            "IMMEDIATE."
        ),
        token_count=65,
        aged_hours=20 * 24,
        tags=("B3",),
    ),
    # ── Type B4: Voyage rate limiting ──
    FixtureLeaf(
        summary_id="sum_b4_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Voyage rate limiting: 429 responses honored via Retry-After "
            "header up to LOCK_BUDGET_AWARE_RETRY_MS=60s. Combined with "
            "voyageMaxRetries=1 and 30s timeout, worst-case is 30+60+30=120s "
            "which exceeds 90s lock TTL. Wave-9 P2 noted this; correctness "
            "preserved by DELETE-before-INSERT but Voyage spend doubles on "
            "storms."
        ),
        token_count=80,
        aged_hours=2 * 24,
        tags=("B4", "D4"),
    ),
    # ── Type D2: operator-VM customer entity (5 leaves) ──
    *_d2_leaves(),
    # ── Type D4: Voyage entity (6 leaves) ──
    *_d4_leaves(),
    # ── Type E1: lineage for "+52.5pp" claim ──
    FixtureLeaf(
        summary_id="sum_e1_001",
        conversation_id=1,
        session_key="agent:main:main",
        content=(
            "Phase A spike result: voyage-4-large + rerank-2.5 lifted "
            "paraphrastic recall by +52.5pp over FTS-only on the "
            "eva-baseline-v2 set (n=8 paraphrastic queries, top-5 relevance "
            "grading). Cost: $0.58 total. Decision: SHIP §1+§2 as designed."
        ),
        token_count=70,
        aged_hours=25 * 24,
        tags=("E1",),
    ),
    # ── Type A1: yesterday (6 leaves) ──
    *_a1_leaves(),
    # ── Type A3: week of April 26-May 2 (8 leaves) ──
    *_a3_leaves(),
    # ── CJK content (Wave-9 P1.4 regression coverage) ──
    FixtureLeaf(
        summary_id="sum_cjk_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Discussion of 机器学习 (machine learning) approaches to entity "
            "coreference. Considered transformer-based vs heuristic "
            "clustering. Decided against ML path for v4.1 due to cost."
        ),
        token_count=45,
        aged_hours=9 * 24,
        tags=("CJK", "B-paraphrase"),
    ),
    FixtureLeaf(
        summary_id="sum_cjk_002",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Eva said: 我们应该测试一下 (we should test this) the new period "
            "mode against synthesize_around. Bilingual test fixture for "
            "verbatim mode CJK regression."
        ),
        token_count=35,
        aged_hours=4 * 24,
        tags=("CJK", "C-cjk"),
    ),
    # ── Suppressed leaves (verify suppression filter on read paths) ──
    FixtureLeaf(
        summary_id="sum_suppressed_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "SENSITIVE — purged via /lcm purge after audit. Should never "
            "appear in any agent-facing read path."
        ),
        token_count=20,
        aged_hours=2 * 24,
        suppressed=True,
        tags=("suppression-filter",),
    ),
    FixtureLeaf(
        summary_id="sum_suppressed_002",
        conversation_id=3,
        session_key="agent:operator-vm:main",
        content=("PII — customer PII redacted via /lcm purge. Should never surface to agent."),
        token_count=15,
        aged_hours=5 * 24,
        suppressed=True,
        tags=("suppression-filter",),
    ),
    # ── Legacy thread (tests session_key scoping with legacy: prefix) ──
    FixtureLeaf(
        summary_id="sum_legacy_001",
        conversation_id=4,
        session_key="legacy:conv_503",
        content=(
            "Legacy thread leaf — should be scoped out of agent:main:main "
            "searches but visible when targeting legacy: prefix."
        ),
        token_count=25,
        aged_hours=50 * 24,
        tags=("session-scope",),
    ),
    # ──────────────────────────────────────────────────────────────────
    # ADVERSARIAL FIXTURE LEAVES (Wave-10 sub-agent #3)
    #
    # These leaves exist to make adversarial scenarios non-trivial. They
    # DO NOT contain the literal phrases adversarial tests query for —
    # that's the whole point. They carry semantically-related content
    # (paraphrase tests) or boundary content (ranking/negative tests) or
    # attack content (adversarial-content tests).
    # ──────────────────────────────────────────────────────────────────
    # ── Paraphrase target #1: "rebase blew up" / "merge mess" ──
    FixtureLeaf(
        summary_id="sum_adv_paraphrase_rebase_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "After the rebase blew up Tuesday morning we had to manually "
            "unwind 14 commits. Took 2 hours. Lesson learned: always rebase "
            "off origin/main, not the local stale tip."
        ),
        token_count=35,
        aged_hours=18 * 24,
        tags=("adv-paraphrase", "B-merge-mess"),
    ),
    # ── Paraphrase target #2: "rollup-replacement tool" semantic phrasing ──
    FixtureLeaf(
        summary_id="sum_adv_paraphrase_lcmrecent_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "We replaced the periodic rollup tool with synthesize_around in "
            "period mode. The deprecated tool used pre-built daily/weekly "
            "aggregates which were always stale. Period mode builds fresh "
            "on-demand from leaves."
        ),
        token_count=45,
        aged_hours=7 * 24,
        tags=("adv-paraphrase", "B-rollup-replacement"),
    ),
    # ── Compound query: time + topic + entity ──
    FixtureLeaf(
        summary_id="sum_adv_compound_purge_recent_001",
        conversation_id=3,
        session_key="agent:operator-vm:main",
        content=(
            "Operator-VM customer escalation: ran /lcm purge --apply on 6 "
            "messages at customer's request. Per the redaction policy, "
            "purge cascaded only to leaves where ALL referencing message "
            "ids were in the purge set."
        ),
        token_count=50,
        aged_hours=18,  # recent — within last 24h
        tags=("adv-compound", "purge", "operator-vm-recent"),
    ),
    # ── Compound: time + topic ──
    FixtureLeaf(
        summary_id="sum_adv_compound_voyage_lastweek_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Voyage backfill cycle Tuesday-Thursday last week: 12K leaves "
            "embedded, 2.4M tokens spent. Rate-limit storms hit on "
            "Wednesday. Final report: 99.4% coverage, 0.6% failed retries."
        ),
        token_count=55,
        aged_hours=8 * 24,  # 8 days ago — within "last week" if anchor is BASE_DATE
        tags=("adv-compound", "voyage-lastweek"),
    ),
    # ── Negative query workaround ──
    FixtureLeaf(
        summary_id="sum_adv_negative_rebase_norace_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Rebase work today: pulled in 47 commits from origin/main, "
            "resolved 3 conflicts in src/store/summary-store.ts. Standard "
            "rebase workflow, nothing exotic."
        ),
        token_count=35,
        aged_hours=30,  # recent
        tags=("adv-negative", "rebase-only"),
    ),
    # And a leaf that DOES mention BOTH so the negative query can exclude.
    FixtureLeaf(
        summary_id="sum_adv_negative_rebase_andrace_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Rebase that included the race-fix patch: cherry-picked from "
            "openclaw-pr70071-rebase. Plan-mode regression test added."
        ),
        token_count=40,
        aged_hours=32,
        tags=("adv-negative", "rebase-with-race"),
    ),
    # ── Adversarial content #1: placeholder injection ({{date_range}}) ──
    FixtureLeaf(
        summary_id="sum_adv_inject_placeholder_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Notes: the daily/weekly templates used `{{date_range}}` as a "
            "placeholder, but renderPrompt never substituted it. Fix "
            "tracked. Until shipped, don't reference {{date_range}} in "
            "user-supplied prompts."
        ),
        token_count=50,
        aged_hours=6 * 24,
        tags=("adv-inject", "placeholder"),
    ),
    # ── Adversarial content #2: XML envelope escape ──
    FixtureLeaf(
        summary_id="sum_adv_inject_xml_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Investigated potential XML envelope escape vector. Crafted "
            "leaf containing literal </leaf-content-abc12345> sequence — "
            "stored fine in messages table. Pre-scan in entity-extractor "
            "(Wave-7 P1) defends against extractor confusion."
        ),
        token_count=50,
        aged_hours=4 * 24,
        tags=("adv-inject", "xml-envelope"),
    ),
    # ── Adversarial content #3: HTML/script injection ──
    FixtureLeaf(
        summary_id="sum_adv_inject_script_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            'Test leaf with HTML markup: <script>alert("xss")</script>. '
            "Persisted as text. Agent surface treats this as plain content "
            "— no execution context, no DOM, no eval. The leaf is "
            "searchable for the literal string."
        ),
        token_count=45,
        aged_hours=3 * 24,
        tags=("adv-inject", "html-script"),
    ),
    # ── Ranking sensitivity: same topic, different recency ──
    FixtureLeaf(
        summary_id="sum_adv_rank_rerank_old_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Rerank cost analysis (initial): 600K token budget, $0.12 per "
            "1M tokens. Coverage gap on long queries. Initial assessment."
        ),
        token_count=30,
        aged_hours=25 * 24,  # 25 days ago — old
        tags=("adv-rank", "rerank-old"),
    ),
    FixtureLeaf(
        summary_id="sum_adv_rank_rerank_new_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Rerank follow-up: budget enforcement landed. Token cap at 600K "
            "is now hard-stop, not silent fallback. Resolves the cost issue "
            "identified earlier."
        ),
        token_count=35,
        aged_hours=6,  # 6 hours ago — fresh
        tags=("adv-rank", "rerank-new"),
    ),
    # ── FTS5 stemming sensitivity ──
    FixtureLeaf(
        summary_id="sum_adv_stem_race_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Sailing diagnostic: RACE-condition reported by participant 5 — "
            "they describe wind shifts during the third leg. Unrelated to "
            "software race conditions."
        ),
        token_count=40,
        aged_hours=10 * 24,
        tags=("adv-stem", "race-unrelated"),
    ),
    # ── Cross-tool composition: leaf with messages backing it ──
    FixtureLeaf(
        summary_id="sum_adv_xtool_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Cross-tool test target: describe should return content; "
            "lcm_grep for the unique phrase 'crosstool-marker-X9K2A1' "
            "should also find this leaf. Used by adversarial cross-tool "
            "tests."
        ),
        token_count=40,
        aged_hours=7 * 24,
        tags=("adv-xtool",),
    ),
    # ── Single-leaf with very specific phrase for ranking-by-relevance ──
    FixtureLeaf(
        summary_id="sum_adv_rank_relevance_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Detailed Voyage rerank-2.5 evaluation: Voyage rerank-2.5 lifts "
            "paraphrastic recall by +52.5pp. Voyage rerank-2.5 is the "
            "production choice. Voyage rerank-2.5 token budget is 600K."
        ),
        token_count=50,
        aged_hours=35 * 24,  # OLD — but ranks #1 by relevance (repeated matches)
        tags=("adv-rank-relevance",),
    ),
)


# ---------------------------------------------------------------------------
# Condensed summary rows (ports v41-test-corpus.ts:585-614)
# ---------------------------------------------------------------------------

FIXTURE_CONDENSED: tuple[FixtureCondensed, ...] = (
    FixtureCondensed(
        summary_id="sum_cond_week_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Week of April 26-May 2 condensed: rebase fix landed (commit "
            "1081067476), race-fix verified, Wave-7 audit completed."
        ),
        token_count=30,
        aged_hours=7 * 24,
        child_ids=("sum_a3_001", "sum_a3_002", "sum_a3_003", "sum_a3_004"),
        tags=("A3", "E"),
    ),
    FixtureCondensed(
        summary_id="sum_cond_voyage_001",
        conversation_id=2,
        session_key="agent:main:main",
        content=(
            "Voyage discussion condensed: model selection (voyage-4-large), "
            "rerank policy, retry budgets, rate-limit handling."
        ),
        token_count=28,
        aged_hours=7 * 24,
        child_ids=(
            "sum_d4_001",
            "sum_d4_002",
            "sum_d4_003",
            "sum_b2_001",
            "sum_b4_001",
        ),
        tags=("D4", "E"),
    ),
)


# ---------------------------------------------------------------------------
# Entity rows + mentions (ports v41-test-corpus.ts:629-678)
# ---------------------------------------------------------------------------

FIXTURE_ENTITIES: tuple[FixtureEntity, ...] = (
    FixtureEntity(
        entity_id="ent_operator_vm",
        session_key="agent:operator-vm:main",
        canonical_text="operator-VM customer",
        entity_type="customer",
        occurrence_count=6,
        mentioned_in=(
            "sum_c3_001",
            "sum_d2_001",
            "sum_d2_002",
            "sum_d2_003",
            "sum_d2_004",
            "sum_d2_005",
        ),
    ),
    FixtureEntity(
        entity_id="ent_voyage",
        session_key="agent:main:main",
        canonical_text="Voyage",
        entity_type="vendor",
        occurrence_count=8,
        mentioned_in=(
            "sum_b2_001",
            "sum_b4_001",
            "sum_d4_001",
            "sum_d4_002",
            "sum_d4_003",
            "sum_d4_004",
            "sum_d4_005",
            "sum_d4_006",
        ),
    ),
    FixtureEntity(
        entity_id="ent_pr_613",
        session_key="agent:main:main",
        canonical_text="PR #613",
        entity_type="pr_number",
        occurrence_count=3,
        mentioned_in=("sum_c2_001", "sum_a1_001", "sum_a1_002"),
    ),
    FixtureEntity(
        entity_id="ent_lcm_recent",
        session_key="agent:main:main",
        canonical_text="lcm_recent",
        entity_type="tool_name",
        occurrence_count=3,
        mentioned_in=("sum_c1_001", "sum_c2_001", "sum_b3_001"),
    ),
)


# ---------------------------------------------------------------------------
# Corpus builder (ports v41-test-corpus.ts:687-857)
# ---------------------------------------------------------------------------


def _all_leaves() -> tuple[FixtureLeaf, ...]:
    """Return every leaf, including the array-generated D2/D4/A1/A3 ones."""
    return FIXTURE_LEAVES


def build_test_corpus(db: sqlite3.Connection) -> CorpusMetadata:
    """Seed the synthetic test corpus into ``db``.

    Faithful Python port of TS ``buildTestCorpus``
    (``v41-test-corpus.ts:687-857``). Runs the migration ladder itself,
    then inserts conversations, leaf-backing messages, leaf summaries,
    condensed summaries + parent links, and entities + mentions.

    The ``db`` connection must be in autocommit mode (``isolation_level
    = None``) — :func:`run_lcm_migrations` opens its own ``BEGIN
    EXCLUSIVE`` and raises if a transaction is already open. Open the
    connection via :func:`lossless_hermes.db.connection.open_lcm_db` (or
    ``sqlite3.connect(path, isolation_level=None)``) before calling this.

    Determinism caveat: this mirrors the TS source exactly, including its
    two ``datetime('now')``-derived columns — the suppressed-message
    ``suppressed_at`` and the entity-table ``first_seen_at`` /
    ``last_seen_at``. Every other timestamp is :data:`BASE_DATE`-relative
    and byte-stable. The benchmark does not read those three columns, so
    the corpus the benchmark queries IS fully deterministic.

    Args:
        db: An open, autocommit-mode :class:`sqlite3.Connection`.

    Returns:
        A :class:`CorpusMetadata` dict describing what was inserted.
    """
    # Ensure schema present. fts5_available=True so summaries_fts /
    # messages_fts exist for the FTS-only retrieval arm. The TS source
    # calls runLcmMigrations(db, { fts5Available: true, ... }) here.
    run_lcm_migrations(db, fts5_available=True, seed_default_prompts=False)

    leaves = _all_leaves()

    # 1. Conversations.
    for conv in FIXTURE_CONVERSATIONS:
        db.execute(
            "INSERT OR IGNORE INTO conversations "
            "(conversation_id, session_id, session_key, active, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                conv.conversation_id,
                conv.session_id,
                conv.session_key,
                conv.active,
                conv.created_at,
            ),
        )

    # 2. Messages backing each leaf. For verbatim tests we need real
    #    message rows: each leaf gets 1 user message carrying the leaf
    #    content. (The TS comment mentions a 3-message plan but the
    #    shipped TS loop inserts exactly 1 user message per leaf — this
    #    port matches the shipped behavior, not the stale comment.)
    message_id = 1
    seq = 1
    for leaf in leaves:
        msg_id = message_id
        message_id += 1
        db.execute(
            "INSERT INTO messages "
            "(message_id, conversation_id, seq, role, content, token_count, "
            " created_at, identity_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                leaf.conversation_id,
                seq,
                "user",
                leaf.content,
                leaf.token_count,
                _time_ago(int(leaf.aged_hours * _HOUR)),
                f"fixture_msg_{msg_id}",
            ),
        )
        seq += 1
        # messages_fts is rowid-indexed fts5(content, ...) — insert by rowid.
        # Suppressed leaves: skip the FTS row so verbatim search can't
        # return them (parity with production FTS triggers).
        if not leaf.suppressed:
            db.execute(
                "INSERT INTO messages_fts(rowid, content) VALUES (?, ?)",
                (msg_id, leaf.content),
            )
        else:
            db.execute(
                "UPDATE messages SET suppressed_at = datetime('now') WHERE message_id = ?",
                (msg_id,),
            )

    # 3. Leaf summaries.
    for leaf in leaves:
        created_at = _time_ago(int(leaf.aged_hours * _HOUR))
        db.execute(
            "INSERT INTO summaries "
            "(summary_id, conversation_id, session_key, kind, depth, content, "
            " token_count, created_at, latest_at, suppressed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                leaf.summary_id,
                leaf.conversation_id,
                leaf.session_key,
                "leaf",
                0,
                leaf.content,
                leaf.token_count,
                created_at,
                created_at,  # latest_at == created_at for leaves
                created_at if leaf.suppressed else None,
            ),
        )
        # Wave-10 sub-agent #3 fix (ported verbatim from v41-test-corpus.ts:
        # 754-765): summaries_fts is fts5(summary_id UNINDEXED, content) —
        # insert via (summary_id, content), NOT (rowid, content). Inserting
        # by rowid leaves summary_id NULL, so the JOIN in search_summaries
        # (summaries_fts.summary_id = s.summary_id) finds nothing and the
        # summary FTS path returns 0 results. Don't insert FTS rows for
        # suppressed leaves — search_summaries filters s.suppressed_at IS
        # NULL post-JOIN, so this is parity with production FTS triggers.
        if not leaf.suppressed:
            db.execute(
                "INSERT INTO summaries_fts(summary_id, content) VALUES (?, ?)",
                (leaf.summary_id, leaf.content),
            )
            # Also index into the CJK trigram table so trigram-tokenized
            # CJK substring search resolves these leaves. Production's
            # SummaryStore.insert_summary indexes both tables; the TS
            # fixture predates summaries_fts_cjk being load-bearing, but
            # mirroring insert_summary keeps CJK queries (eva-fe-013,
            # eva-p-007) working on the FTS-only arm.
            try:
                db.execute(
                    "INSERT INTO summaries_fts_cjk(summary_id, content) VALUES (?, ?)",
                    (leaf.summary_id, leaf.content),
                )
            except sqlite3.DatabaseError:
                # Trigram tokenizer not available on this build — CJK
                # search degrades to LIKE fallback, same as production.
                pass

    # 4. Condensed summaries + parent links.
    for cond in FIXTURE_CONDENSED:
        created_at = _time_ago(int(cond.aged_hours * _HOUR))
        db.execute(
            "INSERT INTO summaries "
            "(summary_id, conversation_id, session_key, kind, depth, content, "
            " token_count, created_at, latest_at, suppressed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cond.summary_id,
                cond.conversation_id,
                cond.session_key,
                "condensed",
                1,
                cond.content,
                cond.token_count,
                created_at,
                created_at,
                None,
            ),
        )
        db.execute(
            "INSERT INTO summaries_fts(summary_id, content) VALUES (?, ?)",
            (cond.summary_id, cond.content),
        )
        try:
            db.execute(
                "INSERT INTO summaries_fts_cjk(summary_id, content) VALUES (?, ?)",
                (cond.summary_id, cond.content),
            )
        except sqlite3.DatabaseError:
            pass
        # Wire parent/child relationships. ordinal is NOT NULL — it's the
        # child's position within the parent.
        for ordinal, child_id in enumerate(cond.child_ids):
            db.execute(
                "INSERT INTO summary_parents "
                "(summary_id, parent_summary_id, ordinal) VALUES (?, ?, ?)",
                (child_id, cond.summary_id, ordinal),
            )

    # 5. Entities + mentions.
    mention_id = 1
    leaves_by_id = {leaf.summary_id: leaf for leaf in leaves}
    for ent in FIXTURE_ENTITIES:
        db.execute(
            "INSERT INTO lcm_entities "
            "(entity_id, session_key, canonical_text, entity_type, "
            " occurrence_count, alternate_surfaces, first_seen_at, "
            " last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now', '-30 days'), "
            "        datetime('now'))",
            (
                ent.entity_id,
                ent.session_key,
                ent.canonical_text,
                ent.entity_type,
                ent.occurrence_count,
                "[]",  # alternate_surfaces
            ),
        )
        for sum_id in ent.mentioned_in:
            leaf = leaves_by_id.get(sum_id)
            if leaf is None:
                continue
            created_at = _time_ago(int(leaf.aged_hours * _HOUR))
            db.execute(
                "INSERT INTO lcm_entity_mentions "
                "(mention_id, entity_id, summary_id, surface_form, "
                " span_start, span_end, mentioned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"mention_{mention_id}",
                    ent.entity_id,
                    sum_id,
                    ent.canonical_text,
                    0,
                    len(ent.canonical_text),
                    created_at,
                ),
            )
            mention_id += 1

    return CorpusMetadata(
        base_date=BASE_DATE,
        conversations=FIXTURE_CONVERSATIONS,
        leaf_count=len(leaves),
        condensed_count=len(FIXTURE_CONDENSED),
        entity_count=len(FIXTURE_ENTITIES),
        suppressed_count=sum(1 for leaf in leaves if leaf.suppressed),
        leaf_summary_ids=tuple(leaf.summary_id for leaf in leaves),
        condensed_summary_ids=tuple(c.summary_id for c in FIXTURE_CONDENSED),
    )
