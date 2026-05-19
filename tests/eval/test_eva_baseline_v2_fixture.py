"""Sanity tests for the eva-baseline-v2 eval fixture (issue 09-05).

Validates ``tests/fixtures/eva_baseline_v2.py``:

* Count is exactly 31.
* Stratum distribution is 14 fts-easy / 9 fts-medium / 8 paraphrastic.
* All ``query_id``s are unique and follow the ``eva-<initials>-NNN``
  convention.
* All ``query_text`` strings are non-empty.
* Every paraphrastic query carries ≥1 ``expected_summary_id`` (the
  paraphrastic stratum is the +52.5pp line — no ground-truth, nothing to
  measure).
* Every ``expected_summary_id`` resolves to a leaf in the synthetic
  corpus (fail-fast on corpus drift — 09-05 risk #3).
* No PII pattern (email / SSN / phone) appears in any ``query_text``
  (defence-in-depth — Path B carries no PII, but the scan stays).
* Register → get round-trips: the DB returns exactly what the builder
  produced.

Provenance: the fixture took **Path B** (rebuilt from
``v41-test-corpus``) — see the ``eva_baseline_v2`` module docstring.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter

import pytest

from lossless_hermes.eval.query_set import (
    QueryRecord,
    encode_query_set_id,
    get_query_set,
)
from tests.fixtures.eva_baseline_v2 import (
    CORPUS_SUMMARY_IDS,
    EVA_BASELINE_V2_IDENTITY,
    build_eva_baseline_v2,
)

# The fixture under test ships a pytest fixture (``eva_baseline_v2_-
# registered``); importing it into this module's namespace makes it
# discoverable for the round-trip test below.
from tests.fixtures.eva_baseline_v2 import eva_baseline_v2_registered  # noqa: F401

# Expected stratum distribution — the upstream Phase-A spike's 14/9/8.
EXPECTED_STRATUM_COUNTS = {"fts-easy": 14, "fts-medium": 9, "paraphrastic": 8}
EXPECTED_TOTAL = 31


@pytest.fixture(scope="module")
def queries() -> list[QueryRecord]:
    """The built query set — module-scoped (the builder is pure)."""
    return build_eva_baseline_v2()


# ---------------------------------------------------------------------------
# Count + stratum distribution
# ---------------------------------------------------------------------------


class TestShape:
    def test_count_is_31(self, queries: list[QueryRecord]) -> None:
        assert len(queries) == EXPECTED_TOTAL

    def test_stratum_distribution_14_9_8(self, queries: list[QueryRecord]) -> None:
        counts = Counter(q.stratum for q in queries)
        assert dict(counts) == EXPECTED_STRATUM_COUNTS

    def test_only_valid_strata(self, queries: list[QueryRecord]) -> None:
        valid = {"fts-easy", "fts-medium", "paraphrastic"}
        assert all(q.stratum in valid for q in queries)


# ---------------------------------------------------------------------------
# query_id integrity
# ---------------------------------------------------------------------------


class TestQueryIds:
    def test_all_query_ids_unique(self, queries: list[QueryRecord]) -> None:
        ids = [q.query_id for q in queries]
        assert len(ids) == len(set(ids))

    def test_query_ids_returned_in_sorted_order(self, queries: list[QueryRecord]) -> None:
        # ``build_eva_baseline_v2`` documents that it returns queries in
        # ``query_id`` order; ``get_query_set`` also sorts by query_id —
        # asserting here keeps the builder honest.
        ids = [q.query_id for q in queries]
        assert ids == sorted(ids)

    def test_query_ids_follow_naming_convention(self, queries: list[QueryRecord]) -> None:
        # eva-fe-NNN / eva-fm-NNN / eva-p-NNN, matching the query's stratum.
        prefix_for = {
            "fts-easy": "eva-fe-",
            "fts-medium": "eva-fm-",
            "paraphrastic": "eva-p-",
        }
        pattern = re.compile(r"^eva-(fe|fm|p)-\d{3}$")
        for q in queries:
            assert pattern.match(q.query_id), f"bad query_id format: {q.query_id}"
            assert q.query_id.startswith(prefix_for[q.stratum]), (
                f"{q.query_id} prefix does not match stratum {q.stratum}"
            )


# ---------------------------------------------------------------------------
# query_text + ground-truth integrity
# ---------------------------------------------------------------------------


class TestQueryContent:
    def test_all_query_text_non_empty(self, queries: list[QueryRecord]) -> None:
        for q in queries:
            assert q.query_text, f"{q.query_id} has empty query_text"
            assert q.query_text.strip() == q.query_text, (
                f"{q.query_id} query_text has surrounding whitespace"
            )

    def test_paraphrastic_queries_have_expected_ids(self, queries: list[QueryRecord]) -> None:
        # 09-05 AC: paraphrastic is the +52.5pp stratum — without
        # ground-truth there is nothing to measure.
        para = [q for q in queries if q.stratum == "paraphrastic"]
        assert len(para) == 8
        for q in para:
            assert q.expected_summary_ids, (
                f"paraphrastic query {q.query_id} has no expected_summary_ids"
            )

    def test_all_expected_ids_resolve_in_corpus(self, queries: list[QueryRecord]) -> None:
        # 09-05 risk #3: ground-truth IDs lock the fixture to the
        # v41-test-corpus shape. This fails fast if a leaf is renamed.
        #
        # NOTE: ``CORPUS_SUMMARY_IDS`` is a hand-mirrored snapshot of
        # ``v41-test-corpus.ts`` (LCM 1f07fbd). Once Epic 03 ports the
        # corpus to Python, re-point this assertion at the live
        # ``test_corpus`` pytest fixture's seeded summary IDs.
        for q in queries:
            if not q.expected_summary_ids:
                continue
            for sid in q.expected_summary_ids:
                assert sid in CORPUS_SUMMARY_IDS, (
                    f"{q.query_id}: expected_summary_id {sid!r} is not a "
                    f"leaf in the v41-test-corpus snapshot"
                )

    def test_no_expected_id_points_at_suppressed_leaf(self, queries: list[QueryRecord]) -> None:
        # Suppressed leaves are filtered out of every agent-facing read
        # path — using one as ground-truth would make recall un-passable.
        suppressed = {"sum_suppressed_001", "sum_suppressed_002"}
        for q in queries:
            if not q.expected_summary_ids:
                continue
            overlap = set(q.expected_summary_ids) & suppressed
            assert not overlap, f"{q.query_id} expects suppressed leaf(s): {sorted(overlap)}"

    def test_every_stratum_has_at_least_one_query_with_ground_truth(
        self, queries: list[QueryRecord]
    ) -> None:
        # Recall is computed per-stratum; a stratum with zero
        # ground-truth queries would produce an empty aggregate.
        for stratum in EXPECTED_STRATUM_COUNTS:
            with_gt = [q for q in queries if q.stratum == stratum and q.expected_summary_ids]
            assert with_gt, f"stratum {stratum} has no query with expected_summary_ids"


# ---------------------------------------------------------------------------
# PII defence-in-depth
# ---------------------------------------------------------------------------


class TestNoPII:
    """Path B carries no PII (synthetic corpus, hand-authored text), but
    a regex scan stays as defence-in-depth — if a future edit pastes a
    real address in, this catches it."""

    EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
    # Common US/intl phone shapes: (123) 456-7890, 123-456-7890,
    # +1 123 456 7890, 123.456.7890. A *separator* (space / dot / dash)
    # or a parenthesised area code is required — a bare run of 10 digits
    # is NOT treated as a phone number (e.g. the commit hash 1081067476
    # in eva-fe-005 is genuine corpus content, not PII).
    PHONE = re.compile(
        r"(?:\+?\d{1,3}[\s.-]+)?"  # optional country code, separator required
        r"(?:\(\d{3}\)\s*|\d{3}[\s.-]+)"  # area code: parens, or 3 digits + sep
        r"\d{3}[\s.-]+\d{4}\b"  # exchange + line, separator required
    )

    def test_no_email_in_query_text(self, queries: list[QueryRecord]) -> None:
        for q in queries:
            assert not self.EMAIL.search(q.query_text), (
                f"{q.query_id}: query_text looks like it contains an email"
            )

    def test_no_ssn_in_query_text(self, queries: list[QueryRecord]) -> None:
        for q in queries:
            assert not self.SSN.search(q.query_text), (
                f"{q.query_id}: query_text looks like it contains an SSN"
            )

    def test_no_phone_in_query_text(self, queries: list[QueryRecord]) -> None:
        for q in queries:
            assert not self.PHONE.search(q.query_text), (
                f"{q.query_id}: query_text looks like it contains a phone number"
            )


# ---------------------------------------------------------------------------
# Identity + DB round-trip
# ---------------------------------------------------------------------------


class TestIdentityAndRoundTrip:
    def test_identity_encodes_to_eva_baseline_v2(self) -> None:
        assert encode_query_set_id(EVA_BASELINE_V2_IDENTITY) == "eva-baseline@v2"

    def test_register_get_round_trips(self, eva_baseline_v2_registered: sqlite3.Connection) -> None:
        # The fixture has already called register_query_set; verify the
        # DB returns exactly what the builder produced.
        loaded = get_query_set(eva_baseline_v2_registered, EVA_BASELINE_V2_IDENTITY)
        assert loaded is not None
        assert loaded.identity == EVA_BASELINE_V2_IDENTITY

        built = sorted(build_eva_baseline_v2(), key=lambda q: q.query_id)
        got = sorted(loaded.queries, key=lambda q: q.query_id)
        assert len(got) == EXPECTED_TOTAL
        # QueryRecord is a frozen dataclass — equality is field-wise.
        assert got == built

    def test_fixture_is_idempotent(self, eva_baseline_v2_registered: sqlite3.Connection) -> None:
        # Re-registering the same identity + content must be a no-op
        # (the fixture's whole point — see register_query_set semantics).
        from lossless_hermes.eval.query_set import register_query_set

        register_query_set(
            eva_baseline_v2_registered,
            EVA_BASELINE_V2_IDENTITY,
            build_eva_baseline_v2(),
        )
        loaded = get_query_set(eva_baseline_v2_registered, EVA_BASELINE_V2_IDENTITY)
        assert loaded is not None
        assert len(loaded.queries) == EXPECTED_TOTAL


# ---------------------------------------------------------------------------
# Builder determinism
# ---------------------------------------------------------------------------


def test_builder_is_deterministic() -> None:
    """``build_eva_baseline_v2`` returns the same data on every call."""
    first = build_eva_baseline_v2()
    second = build_eva_baseline_v2()
    assert first == second
