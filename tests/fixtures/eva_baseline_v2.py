"""eva-baseline-v2 — the 31-query stratified recall eval fixture.

Issue 09-05 (epic 09 — eval). Ports the *concept* of LCM's
``eva-baseline-v2`` eval set; the query texts + ground-truth IDs are
authored here (this file is the source of truth).

### Provenance — **Path B (rebuild from ``v41-test-corpus``)**

The 09-05 spec offered two routes:

* **Path A** — recover Eva's real ``eva-baseline-v2`` rows from her
  local ``~/.openclaw/lcm.db`` snapshot (≈2.6 GB, private content).
* **Path B** — author an equivalent 31-query stratified set against the
  synthetic ``v41-test-corpus`` fixture.

**Path B was taken.** Path A is not available — Eva's snapshot DB is
private, not checked into upstream LCM, and not present on this machine.
Per the issue's standing guidance, Path B is preferred regardless: it is
self-contained, reproducible in CI, and carries zero PII (so the Path-A
PII-redaction step does not apply — see the PII note below).

The ground-truth ``expected_summary_ids`` reference leaf ``summary_id``
values that the LCM synthetic corpus seeds. Those IDs are defined in:

    lossless-claw/test/fixtures/v41-test-corpus.ts  (commit 1f07fbd, pr-613)

— specifically the ``FIXTURE_LEAVES`` / ``FIXTURE_CONDENSED`` arrays in
``buildTestCorpus()``. The corpus is keyed by *literal* ``summary_id``
strings, so the Python port of that fixture (Epic 03 — not yet landed at
the time of this issue) reproduces the same IDs byte-for-byte.

**Corpus snapshot date:** the IDs were resolved against
``v41-test-corpus.ts`` as of LCM commit ``1f07fbd`` (branch ``pr-613``),
read on 2026-05-19. ``CORPUS_SUMMARY_IDS`` below mirrors that snapshot so
a future maintainer can re-verify the ground-truth without the TS repo.

### Stratum distribution (matches the published 14 / 9 / 8 split)

The upstream Phase-A spike (``docs/v4.1/PR_DESCRIPTION.md`` §"Why Voyage
embeddings") graded 14 fts-easy + 9 fts-medium + 8 paraphrastic = 31.
This fixture reproduces that exact split. Stratum definitions, verbatim
from the TS source comments:

* ``fts-easy`` — query terms appear verbatim in at least one expected
  summary; FTS5 alone should find them.
* ``fts-medium`` — single-token paraphrase or coreference (e.g. "she" →
  a name, "the file" → a filename); FTS5 + stemming finds most, semantic
  lifts the rest.
* ``paraphrastic`` — no surface overlap; pure semantic. FTS5 baseline
  ≈5%; the Voyage rerank is the differentiator (the +52.5pp line).

### query_id convention

``eva-<stratum-initials>-NNN``:

* ``eva-fe-NNN`` — fts-easy
* ``eva-fm-NNN`` — fts-medium
* ``eva-p-NNN``  — paraphrastic

IDs are stable: editing a query's *text* must not renumber it, so
recorded eval runs stay comparable across fixture revisions.

### PII

Path B was taken, so the Path-A PII sweep (emails / real names / SSNs /
external company names) is **not applicable** — every query string here
is hand-authored against a synthetic corpus and contains no personal
data. The fixture's own sanity test (``test_eva_baseline_v2_fixture.py``)
still regex-scans every ``query_text`` for email / SSN / phone shapes as
defence-in-depth. The only proper noun used is "Eva", which is the
project-internal codename for the maintainer persona (it appears
throughout ``v41-test-corpus.ts`` itself) — not a real-world identity.

### Ground-truth caveat (issue 09-05 risk #3)

``expected_summary_ids`` lock this fixture to the ``v41-test-corpus``
corpus shape. If that corpus drifts (Epic 03's Python port renames a
leaf), the IDs here go stale. ``CORPUS_SUMMARY_IDS`` + the
``test_all_expected_ids_exist_in_corpus`` test in
``tests/eval/test_eva_baseline_v2_fixture.py`` fail fast if that happens
— that test should be re-pointed at the live ``test_corpus`` pytest
fixture once Epic 03 lands the corpus port.

See:

* ``epics/09-eval/09-05-evaluation-fixtures.md`` — this issue.
* ``epics/09-eval/09-08-benchmarks.md`` — the +52.5pp benchmark runs on
  this fixture.
* ``lossless-claw/test/fixtures/v41-test-corpus.ts`` — the corpus the
  ground-truth IDs resolve against.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from lossless_hermes.eval.query_set import (
    QueryRecord,
    QuerySetIdentity,
    register_query_set,
)

__all__ = [
    "CORPUS_SUMMARY_IDS",
    "EVA_BASELINE_V2_IDENTITY",
    "build_eva_baseline_v2",
    "eva_baseline_v2_registered",
]


EVA_BASELINE_V2_IDENTITY = QuerySetIdentity(name="eva-baseline", version=2)
"""Identity for the eval set. Encodes to ``eva-baseline@v2``.

The name is ``eva-baseline`` and the version is ``2`` (the upstream set
is *eva-baseline-v2*; "v2" is the schema-level version, not part of the
name — see ``query_set.encode_query_set_id`` and the 09-05 spec's
"Python public API" block, which uses ``{"name": "eva-baseline",
"version": 2}``)."""


# ---------------------------------------------------------------------------
# Corpus ground-truth ID snapshot
# ---------------------------------------------------------------------------
#
# Every ``summary_id`` seeded by ``buildTestCorpus()`` in
# ``lossless-claw/test/fixtures/v41-test-corpus.ts`` (commit 1f07fbd),
# read 2026-05-19. This is the universe that ``expected_summary_ids``
# below may draw from. Kept here so the fixture's ground-truth can be
# audited without the TS repo; re-point the corpus-existence test at the
# live ``test_corpus`` fixture once Epic 03 ports the corpus.
#
# Suppressed leaves (sum_suppressed_001/002) ARE in the corpus but are
# filtered out of every agent-facing read path, so they are deliberately
# NOT used as ground-truth for any query here.
CORPUS_SUMMARY_IDS: frozenset[str] = frozenset({
    # ── Verbatim known-content leaves (Type C1-C5) ──
    "sum_c1_001",
    "sum_c2_001",
    "sum_c3_001",
    "sum_c4_001",
    "sum_c5_001",
    # ── Topic leaves (Type B1-B4) ──
    "sum_b1_001",
    "sum_b2_001",
    "sum_b3_001",
    "sum_b4_001",
    # ── operator-VM customer entity leaves (Type D2) ──
    "sum_d2_001",
    "sum_d2_002",
    "sum_d2_003",
    "sum_d2_004",
    "sum_d2_005",
    # ── Voyage entity leaves (Type D4) ──
    "sum_d4_001",
    "sum_d4_002",
    "sum_d4_003",
    "sum_d4_004",
    "sum_d4_005",
    "sum_d4_006",
    # ── +52.5pp lineage leaf (Type E1) ──
    "sum_e1_001",
    # ── Yesterday's work items (Type A1) ──
    "sum_a1_001",
    "sum_a1_002",
    "sum_a1_003",
    "sum_a1_004",
    "sum_a1_005",
    "sum_a1_006",
    # ── Week-recap items (Type A3) ──
    "sum_a3_001",
    "sum_a3_002",
    "sum_a3_003",
    "sum_a3_004",
    "sum_a3_005",
    "sum_a3_006",
    "sum_a3_007",
    "sum_a3_008",
    # ── CJK regression leaves ──
    "sum_cjk_001",
    "sum_cjk_002",
    # ── Suppressed leaves (present but filtered from reads) ──
    "sum_suppressed_001",
    "sum_suppressed_002",
    # ── Legacy-thread leaf ──
    "sum_legacy_001",
    # ── Adversarial fixture leaves (Wave-10 sub-agent #3) ──
    "sum_adv_paraphrase_rebase_001",
    "sum_adv_paraphrase_lcmrecent_001",
    "sum_adv_compound_purge_recent_001",
    "sum_adv_compound_voyage_lastweek_001",
    "sum_adv_negative_rebase_norace_001",
    "sum_adv_negative_rebase_andrace_001",
    "sum_adv_inject_placeholder_001",
    "sum_adv_inject_xml_001",
    "sum_adv_inject_script_001",
    "sum_adv_rank_rerank_old_001",
    "sum_adv_rank_rerank_new_001",
    "sum_adv_stem_race_001",
    "sum_adv_xtool_001",
    "sum_adv_rank_relevance_001",
    # ── Condensed summaries (parent rows) ──
    "sum_cond_week_001",
    "sum_cond_voyage_001",
})


def build_eva_baseline_v2() -> list[QueryRecord]:
    """Build the canonical eva-baseline-v2 query set (31 queries).

    Returns the queries in ``query_id`` order: 14 fts-easy, then 9
    fts-medium, then 8 paraphrastic. The list is the source of truth —
    tests call this builder; nothing reads the DB rows directly.

    Provenance: **Path B** — see this module's docstring. Each query's
    stratum classification is justified in an inline comment so a
    reviewer can audit the borderline fts-medium / paraphrastic calls.

    Returns:
        The 31 :class:`QueryRecord` instances, deterministic across calls.
    """
    queries: list[QueryRecord] = [
        # ───────────────────────────── fts-easy (14) ─────────────────────
        # fts-easy: query terms appear verbatim in the target summary.
        #
        # eva-fe-001 — "rollups" + "condensed summaries" are literal in
        # sum_c1_001 ("...throw out rollups; they're worse than condensed
        # summaries...").
        QueryRecord(
            query_id="eva-fe-001",
            query_text="throw out rollups worse than condensed summaries",
            stratum="fts-easy",
            expected_summary_ids=("sum_c1_001",),
        ),
        # eva-fe-002 — "synthesize_around period mode" is verbatim in
        # both sum_c1_001 and sum_c2_001.
        QueryRecord(
            query_id="eva-fe-002",
            query_text="synthesize_around period mode decision",
            stratum="fts-easy",
            expected_summary_ids=("sum_c1_001", "sum_c2_001"),
        ),
        # eva-fe-003 — "gateway timeout 30s" + "CPU spike" literal in
        # sum_c3_001 (operator-VM escalation quote).
        QueryRecord(
            query_id="eva-fe-003",
            query_text="customer reported gateway timeout 30s CPU spike",
            stratum="fts-easy",
            expected_summary_ids=("sum_c3_001",),
        ),
        # eva-fe-004 — "VOYAGE_API_KEY not set" is the literal error
        # string in sum_c4_001 (backfill-autostart no-op).
        QueryRecord(
            query_id="eva-fe-004",
            query_text="VOYAGE_API_KEY not set cannot start backfill worker",
            stratum="fts-easy",
            expected_summary_ids=("sum_c4_001",),
        ),
        # eva-fe-005 — commit hash "1081067476" + "empty-plan-body race"
        # literal in sum_c5_001 (verbatim commit message).
        QueryRecord(
            query_id="eva-fe-005",
            query_text="commit 1081067476 persist plan_steps empty-plan-body race",
            stratum="fts-easy",
            expected_summary_ids=("sum_c5_001",),
        ),
        # eva-fe-006 — "worker_threads heartbeat isolation" literal in
        # sum_b1_001.
        QueryRecord(
            query_id="eva-fe-006",
            query_text="worker_threads heartbeat isolation",
            stratum="fts-easy",
            expected_summary_ids=("sum_b1_001",),
        ),
        # eva-fe-007 — "rerank-2.5" + "+52.5pp" literal in sum_b2_001;
        # sum_adv_rank_relevance_001 also repeats "Voyage rerank-2.5".
        QueryRecord(
            query_id="eva-fe-007",
            query_text="Voyage rerank-2.5 paraphrastic recall +52.5pp",
            stratum="fts-easy",
            expected_summary_ids=("sum_b2_001", "sum_adv_rank_relevance_001"),
        ),
        # eva-fe-008 — "429" + "Retry-After" literal in sum_b4_001
        # (Voyage rate-limit handling).
        QueryRecord(
            query_id="eva-fe-008",
            query_text="Voyage 429 responses Retry-After header",
            stratum="fts-easy",
            expected_summary_ids=("sum_b4_001",),
        ),
        # eva-fe-009 — "voyage-4-large" literal in every sum_d4_NNN leaf
        # ("voyage-4-large embedding model + rerank-2.5").
        QueryRecord(
            query_id="eva-fe-009",
            query_text="voyage-4-large embedding model",
            stratum="fts-easy",
            expected_summary_ids=(
                "sum_d4_001",
                "sum_d4_002",
                "sum_d4_003",
                "sum_d4_004",
                "sum_d4_005",
                "sum_d4_006",
            ),
        ),
        # eva-fe-010 — "Phase A spike" + "eva-baseline-v2" literal in
        # sum_e1_001 (the +52.5pp lineage leaf).
        QueryRecord(
            query_id="eva-fe-010",
            query_text="Phase A spike result eva-baseline-v2 set",
            stratum="fts-easy",
            expected_summary_ids=("sum_e1_001",),
        ),
        # eva-fe-011 — "{{date_range}}" literal in sum_adv_inject_-
        # placeholder_001 (placeholder-injection adversarial leaf).
        QueryRecord(
            query_id="eva-fe-011",
            query_text="{{date_range}} placeholder renderPrompt substitution",
            stratum="fts-easy",
            expected_summary_ids=("sum_adv_inject_placeholder_001",),
        ),
        # eva-fe-012 — unique marker phrase "crosstool-marker-X9K2A1"
        # literal in sum_adv_xtool_001 (cross-tool test target).
        QueryRecord(
            query_id="eva-fe-012",
            query_text="crosstool-marker-X9K2A1",
            stratum="fts-easy",
            expected_summary_ids=("sum_adv_xtool_001",),
        ),
        # eva-fe-013 — "机器学习" (machine learning) literal CJK term in
        # sum_cjk_001 (Wave-9 P1.4 CJK regression leaf).
        QueryRecord(
            query_id="eva-fe-013",
            query_text="机器学习 entity coreference",
            stratum="fts-easy",
            expected_summary_ids=("sum_cjk_001",),
        ),
        # eva-fe-014 — "</leaf-content-abc12345>" literal escape sequence
        # in sum_adv_inject_xml_001 (XML-envelope adversarial leaf).
        QueryRecord(
            query_id="eva-fe-014",
            query_text="leaf-content-abc12345 XML envelope escape",
            stratum="fts-easy",
            expected_summary_ids=("sum_adv_inject_xml_001",),
        ),
        # ──────────────────────────── fts-medium (9) ─────────────────────
        # fts-medium: single-token paraphrase / coreference. Some surface
        # words match; one key term is swapped for a synonym or pronoun.
        #
        # eva-fm-001 — "she" → "Eva" coreference; "approved" matches
        # "Approved by Eva" in sum_c2_001 but the subject is a pronoun.
        QueryRecord(
            query_id="eva-fm-001",
            query_text="what decision did she approve about rollups",
            stratum="fts-medium",
            expected_summary_ids=("sum_c2_001",),
        ),
        # eva-fm-002 — "the customer" → "operator-VM customer"; "follow-up"
        # is literal in sum_d2_NNN but "the customer" is a coreference.
        QueryRecord(
            query_id="eva-fm-002",
            query_text="follow-ups with the customer about the timeout",
            stratum="fts-medium",
            expected_summary_ids=(
                "sum_d2_001",
                "sum_d2_002",
                "sum_d2_003",
                "sum_d2_004",
                "sum_d2_005",
            ),
        ),
        # eva-fm-003 — "rate limiting" → "rate-limit"; single-token
        # hyphenation variant. "storms" is literal in sum_b4_001.
        QueryRecord(
            query_id="eva-fm-003",
            query_text="Voyage rate limiting during storms",
            stratum="fts-medium",
            expected_summary_ids=("sum_b4_001",),
        ),
        # eva-fm-004 — "the race fix" → "race-fix patch"; coreference to
        # the patch named in sum_adv_negative_rebase_andrace_001.
        QueryRecord(
            query_id="eva-fm-004",
            query_text="the rebase that pulled in the race fix",
            stratum="fts-medium",
            expected_summary_ids=("sum_adv_negative_rebase_andrace_001",),
        ),
        # eva-fm-005 — "yesterday's audit fixes" → sum_a1_NNN content is
        # "Yesterday's work item ... Wave-9 audit fix"; "audit fix" is
        # literal, "fixes" is a single-token plural variant.
        QueryRecord(
            query_id="eva-fm-005",
            query_text="yesterday's Wave-9 audit fixes",
            stratum="fts-medium",
            expected_summary_ids=(
                "sum_a1_001",
                "sum_a1_002",
                "sum_a1_003",
                "sum_a1_004",
                "sum_a1_005",
                "sum_a1_006",
            ),
        ),
        # eva-fm-006 — "the budget" → "token budget"; "rerank" is literal
        # in sum_b2_001 ("Rerank token budget is 600K").
        QueryRecord(
            query_id="eva-fm-006",
            query_text="is the rerank budget enforced",
            stratum="fts-medium",
            expected_summary_ids=("sum_b2_001",),
        ),
        # eva-fm-007 — "TOCTOU" → "race condition"; sum_b3_001 says
        # "TOCTOU race" — the query uses the long-form synonym
        # "time-of-check" for the acronym.
        QueryRecord(
            query_id="eva-fm-007",
            query_text="time-of-check race condition in reconcileSessionKeys",
            stratum="fts-medium",
            expected_summary_ids=("sum_b3_001",),
        ),
        # eva-fm-008 — "test this" → "我们应该测试一下" (CJK for "we should
        # test this"); cross-language single-phrase coreference to
        # sum_cjk_002.
        QueryRecord(
            query_id="eva-fm-008",
            query_text="Eva said we should test the new period mode",
            stratum="fts-medium",
            expected_summary_ids=("sum_cjk_002",),
        ),
        # eva-fm-009 — "newer rerank notes" → sum_adv_rank_rerank_new_001
        # ("Rerank follow-up: budget enforcement landed"). "rerank" is
        # literal; "follow-up" is paraphrased to "newer notes" — but
        # "budget enforcement" anchors it. Single-token-ish lift.
        QueryRecord(
            query_id="eva-fm-009",
            query_text="newer rerank notes on budget enforcement landing",
            stratum="fts-medium",
            expected_summary_ids=("sum_adv_rank_rerank_new_001",),
        ),
        # ─────────────────────────── paraphrastic (8) ────────────────────
        # paraphrastic: NO surface overlap with the target summary. Pure
        # semantic match — FTS5 baseline ≈5%, Voyage rerank is the
        # differentiator. THIS is the +52.5pp stratum; every paraphrastic
        # query MUST carry expected_summary_ids (09-05 AC).
        #
        # eva-p-001 — target sum_adv_paraphrase_rebase_001 says "the
        # rebase blew up Tuesday morning ... manually unwind 14 commits".
        # Query says "merge went sideways" — zero shared content words
        # ("merge"/"sideways"/"untangle" vs "rebase"/"blew up"/"unwind").
        QueryRecord(
            query_id="eva-p-001",
            query_text="that time the merge went sideways and we had to untangle everything",
            stratum="paraphrastic",
            expected_summary_ids=("sum_adv_paraphrase_rebase_001",),
        ),
        # eva-p-002 — target sum_adv_paraphrase_lcmrecent_001 says "We
        # replaced the periodic rollup tool with synthesize_around".
        # Query asks for "the stale-aggregate feature we got rid of" —
        # semantic only; "ditched"/"stale-aggregate" share no tokens with
        # "replaced"/"periodic rollup".
        QueryRecord(
            query_id="eva-p-002",
            query_text="which stale-aggregate feature did we ditch and what replaced it",
            stratum="paraphrastic",
            expected_summary_ids=("sum_adv_paraphrase_lcmrecent_001",),
        ),
        # eva-p-003 — target sum_e1_001 records the spike "Decision: SHIP
        # §1+§2 as designed". Query asks "what convinced us embeddings
        # were worth the money" — pure intent; no shared surface form
        # with "Phase A spike result ... lifted paraphrastic recall".
        QueryRecord(
            query_id="eva-p-003",
            query_text="what convinced us embeddings were worth the money",
            stratum="paraphrastic",
            expected_summary_ids=("sum_e1_001",),
        ),
        # eva-p-004 — target sum_b1_001 is about "worker_threads heartbeat
        # isolation ... a stuck LLM call cannot block the lock TTL". Query
        # asks "how do we stop a hung model call from killing our lease" —
        # "hung"/"lease" vs "stuck"/"lock TTL", semantic only.
        QueryRecord(
            query_id="eva-p-004",
            query_text="how do we stop a hung model call from killing our lease",
            stratum="paraphrastic",
            expected_summary_ids=("sum_b1_001",),
        ),
        # eva-p-005 — target sum_adv_compound_purge_recent_001 is about
        # running "/lcm purge --apply on 6 messages at customer's
        # request". Query asks "did we scrub anything for the client
        # lately" — "scrub"/"client" vs "purge"/"customer", no overlap.
        QueryRecord(
            query_id="eva-p-005",
            query_text="did we scrub anything for the client lately",
            stratum="paraphrastic",
            expected_summary_ids=("sum_adv_compound_purge_recent_001",),
        ),
        # eva-p-006 — target sum_b3_001 describes "empty-plan-body slipped
        # through because lastPlanSteps was written async". Query asks
        # "why did plans sometimes come through blank" — "blank"/"come
        # through" vs "empty-plan-body"/"slipped through", semantic only.
        QueryRecord(
            query_id="eva-p-006",
            query_text="why did plans sometimes come through blank",
            stratum="paraphrastic",
            expected_summary_ids=("sum_b3_001",),
        ),
        # eva-p-007 — target sum_cjk_001 is about "机器学习 (machine
        # learning) approaches to entity coreference ... Decided against
        # ML path for v4.1 due to cost". Query asks "why didn't we use a
        # neural model to merge duplicate names" — "neural model"/"merge
        # duplicate names" vs "ML path"/"entity coreference", no overlap.
        QueryRecord(
            query_id="eva-p-007",
            query_text="why didn't we use a neural model to merge duplicate names",
            stratum="paraphrastic",
            expected_summary_ids=("sum_cjk_001",),
        ),
        # eva-p-008 — target sum_adv_compound_voyage_lastweek_001 records
        # "Voyage backfill cycle ... 12K leaves embedded ... 99.4%
        # coverage". Query asks "how complete is our vector coverage after
        # the big indexing run" — "vector coverage"/"big indexing run" vs
        # "embedded"/"backfill cycle", semantic only.
        QueryRecord(
            query_id="eva-p-008",
            query_text="how complete is our vector coverage after the big indexing run",
            stratum="paraphrastic",
            expected_summary_ids=("sum_adv_compound_voyage_lastweek_001",),
        ),
    ]
    return queries


@pytest.fixture
def eva_baseline_v2_registered(
    db_in_memory: sqlite3.Connection,
) -> Iterator[sqlite3.Connection]:
    """Register the eva-baseline-v2 set into a fresh in-memory DB.

    Idempotent: calls :func:`register_query_set` once with
    :data:`EVA_BASELINE_V2_IDENTITY` + :func:`build_eva_baseline_v2`.

    The ``db_in_memory`` connection is migrated here (the bare conftest
    fixture yields an un-migrated connection — the migration ladder is
    not auto-applied per ``tests/conftest.py``). After this fixture runs,
    ``get_query_set(db, EVA_BASELINE_V2_IDENTITY)`` round-trips the set.

    Yields:
        The same connection, now carrying the registered query set.
    """
    # The shared ``db_in_memory`` fixture deliberately ships an
    # un-migrated connection (see tests/conftest.py). Apply the migration
    # ladder here so lcm_eval_query_set / lcm_eval_query exist.
    from lossless_hermes.db.migration import run_lcm_migrations

    run_lcm_migrations(
        db_in_memory,
        fts5_available=False,
        seed_default_prompts=False,
    )
    register_query_set(
        db_in_memory,
        EVA_BASELINE_V2_IDENTITY,
        build_eva_baseline_v2(),
    )
    yield db_in_memory
