"""Tests for :mod:`lossless_hermes.tools.expansion_recursion_guard` (06-06).

Ports parity checks for ``lossless-claw/src/tools/lcm-expansion-recursion-guard.ts``
(LCM commit ``1f07fbd`` on branch ``pr-613``).

Coverage:

* :func:`create_expansion_request_id` — fresh UUID4 per call.
* :func:`resolve_expansion_request_id` — inherits stamped id if present.
* :func:`resolve_next_expansion_depth` — 1 for fresh sessions, stamped+1
  for stamped sessions, depth_cap+1 for auth-grant fallback.
* :func:`stamp_delegated_expansion_context` — clamping, blank-origin
  collapse to ``"main"``, empty-session no-op.
* :func:`clear_delegated_expansion_context` — drops both maps.
* :func:`evaluate_expansion_recursion_guard` — allowed branch, depth-cap
  block, idempotent-reentry block, auth-grant fallback.
* :func:`acquire_expansion_concurrency_slot` — first acquire succeeds,
  second concurrent acquire from a different request_id is blocked,
  same-request_id is idempotent allowed.
* :func:`release_expansion_concurrency_slot` — request_id mismatch is a
  no-op; ``request_id=None`` force-releases.
* :func:`record_expansion_delegation_telemetry` — counters increment,
  start/success log INFO, block/timeout log WARN.
* :func:`reset_for_tests` — zeros every state map + counters.
* Concurrency stress — 50 threads acquire the same origin slot in
  parallel; exactly 1 wins, 49 are blocked.

References:

* :mod:`lossless_hermes.tools.expansion_recursion_guard` — implementation.
* ``/Volumes/LEXAR/Claude/lossless-claw/src/tools/lcm-expansion-recursion-guard.ts``
  — TS source.
* ``epics/06-tools/06-06-expansion-recursion-guard.md`` — issue spec.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Iterator

import pytest

from lossless_hermes.tools import expansion_recursion_guard as guard


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Reset module state before AND after each test.

    Module-level dicts persist between tests in a single process. The
    autouse fixture guarantees every test starts from a known-clean
    state and leaves no residue for the next test.
    """
    guard.reset_for_tests()
    guard.set_delegated_grant_resolver(None)  # restore default no-op
    try:
        yield
    finally:
        guard.reset_for_tests()
        guard.set_delegated_grant_resolver(None)


# ---------------------------------------------------------------------------
# create_expansion_request_id
# ---------------------------------------------------------------------------


def test_create_expansion_request_id_is_uuid4_format() -> None:
    """Each call returns a fresh UUID4 string in canonical form."""
    rid = guard.create_expansion_request_id()
    parsed = uuid.UUID(rid)
    assert parsed.version == 4
    assert str(parsed) == rid


def test_create_expansion_request_id_is_unique_across_calls() -> None:
    """100 calls produce 100 distinct ids."""
    ids = {guard.create_expansion_request_id() for _ in range(100)}
    assert len(ids) == 100


# ---------------------------------------------------------------------------
# resolve_expansion_request_id
# ---------------------------------------------------------------------------


def test_resolve_request_id_fresh_session_mints_new() -> None:
    """A session with no stamped context gets a fresh UUID4."""
    rid = guard.resolve_expansion_request_id("foo")
    parsed = uuid.UUID(rid)
    assert parsed.version == 4


def test_resolve_request_id_returns_stamped_id() -> None:
    """A stamped session reuses its stamped request_id."""
    stamped_id = "stamped-id-1234"
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id=stamped_id,
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    assert guard.resolve_expansion_request_id("foo") == stamped_id


def test_resolve_request_id_empty_session_mints_new() -> None:
    """Empty session_key always yields a fresh id (no lookup attempted)."""
    rid = guard.resolve_expansion_request_id("")
    uuid.UUID(rid)  # raises if invalid


def test_resolve_request_id_none_session_mints_new() -> None:
    """``None`` session_key behaves like empty."""
    rid = guard.resolve_expansion_request_id(None)
    uuid.UUID(rid)


# ---------------------------------------------------------------------------
# resolve_next_expansion_depth
# ---------------------------------------------------------------------------


def test_resolve_next_depth_fresh_session_returns_1() -> None:
    """Acceptance criteria: fresh state → 1."""
    assert guard.resolve_next_expansion_depth("foo") == 1


def test_resolve_next_depth_after_stamp_at_1_returns_2() -> None:
    """Acceptance criteria: after stamp at depth 1 → 2."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    assert guard.resolve_next_expansion_depth("foo") == 2


def test_resolve_next_depth_empty_session_returns_1() -> None:
    """Empty session_key short-circuits to 1."""
    assert guard.resolve_next_expansion_depth("") == 1


def test_resolve_next_depth_grant_fallback() -> None:
    """When no stamp exists but the grant resolver returns a grant, depth = cap + 1."""

    def fake_resolver(session_key: str) -> str | None:
        return "grant-1" if session_key == "session-with-grant" else None

    guard.set_delegated_grant_resolver(fake_resolver)
    assert (
        guard.resolve_next_expansion_depth("session-with-grant")
        == guard.EXPANSION_DELEGATION_DEPTH_CAP + 1
    )
    assert guard.resolve_next_expansion_depth("other-session") == 1


# ---------------------------------------------------------------------------
# stamp_delegated_expansion_context
# ---------------------------------------------------------------------------


def test_stamp_clamps_negative_depth_to_zero() -> None:
    """Negative depth is clamped to 0 (parity with TS Math.max(0, …))."""
    ctx = guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=-5,
        origin_session_key="origin",
        stamped_by="test",
    )
    assert ctx.expansion_depth == 0


def test_stamp_truncates_float_depth() -> None:
    """Float depths are integer-truncated."""
    ctx = guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=1.9,  # type: ignore[arg-type]
        origin_session_key="origin",
        stamped_by="test",
    )
    assert ctx.expansion_depth == 1


def test_stamp_blank_origin_collapses_to_main() -> None:
    """Blank origin_session_key becomes ``"main"``."""
    ctx = guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="",
        stamped_by="test",
    )
    assert ctx.origin_session_key == "main"


def test_stamp_empty_session_key_does_not_mutate_state() -> None:
    """Empty session_key returns a context object but skips the state mutation."""
    ctx = guard.stamp_delegated_expansion_context(
        session_key="",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    assert ctx.request_id == "rid-1"
    # State map is untouched — any lookup returns None.
    assert guard.get_delegated_expansion_context_for_tests("") is None


def test_stamp_round_trips_through_test_accessor() -> None:
    """Stamped context is readable via the test accessor."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="lcm_expand_query",
    )
    ctx = guard.get_delegated_expansion_context_for_tests("foo")
    assert ctx is not None
    assert ctx.request_id == "rid-1"
    assert ctx.expansion_depth == 1
    assert ctx.origin_session_key == "origin"
    assert ctx.stamped_by == "lcm_expand_query"


def test_stamp_normalizes_session_key_whitespace() -> None:
    """``session_key`` with surrounding whitespace is stored against the trimmed form."""
    guard.stamp_delegated_expansion_context(
        session_key="  foo  ",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    # The trimmed key is the canonical storage form.
    assert guard.get_delegated_expansion_context_for_tests("foo") is not None
    assert guard.get_delegated_expansion_context_for_tests("  foo  ") is not None


# ---------------------------------------------------------------------------
# clear_delegated_expansion_context
# ---------------------------------------------------------------------------


def test_clear_removes_stamped_context() -> None:
    """After clear, the stamped context is gone."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    guard.clear_delegated_expansion_context("foo")
    assert guard.get_delegated_expansion_context_for_tests("foo") is None


def test_clear_empty_session_is_noop() -> None:
    """Clearing an empty session_key is a silent no-op."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    guard.clear_delegated_expansion_context("")
    assert guard.get_delegated_expansion_context_for_tests("foo") is not None


def test_clear_also_drops_blocked_id_history() -> None:
    """After a block, clear() removes the blocked-id set as well."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    # First evaluation records the blocked request_id.
    guard.evaluate_expansion_recursion_guard(session_key="foo", request_id="rid-block")
    guard.clear_delegated_expansion_context("foo")
    # Re-stamp + re-evaluate same id — should be "depth_cap" (first time),
    # not "idempotent_reentry" (which would mean the blocked-id set
    # survived the clear, indicating a bug).
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-2",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    decision = guard.evaluate_expansion_recursion_guard(session_key="foo", request_id="rid-block")
    assert decision.blocked is True
    assert decision.reason == "depth_cap"


# ---------------------------------------------------------------------------
# evaluate_expansion_recursion_guard
# ---------------------------------------------------------------------------


def test_evaluate_no_stamp_is_allowed() -> None:
    """Fresh session with no stamp → allowed branch."""
    decision = guard.evaluate_expansion_recursion_guard(session_key="foo", request_id="rid-1")
    assert decision.blocked is False
    assert decision.request_id == "rid-1"
    assert decision.expansion_depth == 0
    assert decision.origin_session_key == "foo"


def test_evaluate_empty_session_no_stamp_uses_main_origin() -> None:
    """Empty session_key with no stamp → allowed, origin=``"main"``."""
    decision = guard.evaluate_expansion_recursion_guard(session_key="", request_id="rid-1")
    assert decision.blocked is False
    assert decision.origin_session_key == "main"


def test_evaluate_below_depth_cap_is_allowed() -> None:
    """Stamped depth 0 (below cap=1) → allowed."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=0,
        origin_session_key="origin",
        stamped_by="test",
    )
    decision = guard.evaluate_expansion_recursion_guard(session_key="foo", request_id="rid-1")
    assert decision.blocked is False
    assert decision.expansion_depth == 0
    assert decision.origin_session_key == "origin"


def test_evaluate_at_depth_cap_blocks_with_depth_cap_reason() -> None:
    """Acceptance criteria: stamped at depth 1, first eval → depth_cap block."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="stamped-rid",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    decision = guard.evaluate_expansion_recursion_guard(session_key="foo", request_id="new-rid")
    assert decision.blocked is True
    assert decision.reason == "depth_cap"
    assert decision.code == guard.EXPANSION_RECURSION_ERROR_CODE
    assert decision.expansion_depth == 1
    assert decision.origin_session_key == "origin"
    assert "Recovery:" in decision.message
    assert "origin" in decision.message


def test_evaluate_idempotent_reentry_reports_reason() -> None:
    """Acceptance criteria: same request_id twice → idempotent_reentry block."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="stamped-rid",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    first = guard.evaluate_expansion_recursion_guard(session_key="foo", request_id="duplicate")
    second = guard.evaluate_expansion_recursion_guard(session_key="foo", request_id="duplicate")
    assert first.blocked is True
    assert first.reason == "depth_cap"
    assert second.blocked is True
    assert second.reason == "idempotent_reentry"


def test_evaluate_grant_fallback_blocks_at_depth_cap() -> None:
    """When auth grant exists but no stamp, treat as stamped at depth cap."""

    def fake_resolver(session_key: str) -> str | None:
        return "grant-1" if session_key == "foo" else None

    guard.set_delegated_grant_resolver(fake_resolver)
    decision = guard.evaluate_expansion_recursion_guard(session_key="foo", request_id="rid-1")
    assert decision.blocked is True
    assert decision.reason == "depth_cap"
    assert decision.origin_session_key == "foo"


def test_evaluate_strips_request_id_whitespace() -> None:
    """``request_id`` is trimmed before use."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="stamped",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    decision = guard.evaluate_expansion_recursion_guard(
        session_key="foo", request_id="  trimmed-rid  "
    )
    assert decision.blocked is True
    assert decision.request_id == "trimmed-rid"


# ---------------------------------------------------------------------------
# acquire_expansion_concurrency_slot
# ---------------------------------------------------------------------------


def test_acquire_first_caller_wins() -> None:
    """First caller from an origin gets the slot."""
    decision = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-1", request_id="rid-1"
    )
    assert decision.blocked is False
    assert decision.request_id == "rid-1"
    assert decision.origin_session_key == "origin-1"


def test_acquire_second_caller_blocked() -> None:
    """Acceptance criteria: second concurrent acquire from same origin → blocked."""
    guard.acquire_expansion_concurrency_slot(origin_session_key="origin-1", request_id="rid-1")
    decision = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-1", request_id="rid-2"
    )
    assert decision.blocked is True
    assert decision.reason == "origin_session_in_flight"
    assert decision.code == guard.EXPANSION_CONCURRENCY_ERROR_CODE
    assert "origin-1" in decision.message
    assert "Recovery:" in decision.message


def test_acquire_idempotent_same_request_id() -> None:
    """Re-acquiring with the same request_id is allowed (idempotent)."""
    guard.acquire_expansion_concurrency_slot(origin_session_key="origin-1", request_id="rid-1")
    decision = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-1", request_id="rid-1"
    )
    assert decision.blocked is False


def test_acquire_different_origins_dont_collide() -> None:
    """Two different origins each get their own slot."""
    first = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-1", request_id="rid-1"
    )
    second = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-2", request_id="rid-2"
    )
    assert first.blocked is False
    assert second.blocked is False


def test_acquire_blank_origin_collapses_to_main() -> None:
    """Blank origin_session_key becomes ``"main"`` for slot tracking."""
    first = guard.acquire_expansion_concurrency_slot(origin_session_key="", request_id="rid-1")
    second = guard.acquire_expansion_concurrency_slot(origin_session_key=None, request_id="rid-2")
    assert first.blocked is False
    assert first.origin_session_key == "main"
    assert second.blocked is True
    assert second.origin_session_key == "main"


# ---------------------------------------------------------------------------
# release_expansion_concurrency_slot
# ---------------------------------------------------------------------------


def test_release_frees_slot_for_next_acquire() -> None:
    """After release, a new caller can acquire."""
    guard.acquire_expansion_concurrency_slot(origin_session_key="origin-1", request_id="rid-1")
    guard.release_expansion_concurrency_slot(origin_session_key="origin-1", request_id="rid-1")
    decision = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-1", request_id="rid-2"
    )
    assert decision.blocked is False


def test_release_mismatched_request_id_is_noop() -> None:
    """Releasing with a wrong request_id does NOT free the slot."""
    guard.acquire_expansion_concurrency_slot(origin_session_key="origin-1", request_id="rid-1")
    guard.release_expansion_concurrency_slot(origin_session_key="origin-1", request_id="wrong-rid")
    # The slot should still be held by rid-1.
    decision = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-1", request_id="rid-2"
    )
    assert decision.blocked is True


def test_release_without_request_id_force_releases() -> None:
    """``request_id=None`` force-releases regardless of holder."""
    guard.acquire_expansion_concurrency_slot(origin_session_key="origin-1", request_id="rid-1")
    guard.release_expansion_concurrency_slot(origin_session_key="origin-1", request_id=None)
    decision = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-1", request_id="rid-2"
    )
    assert decision.blocked is False


def test_release_empty_origin_is_noop() -> None:
    """Empty origin_session_key short-circuits without touching state."""
    guard.acquire_expansion_concurrency_slot(origin_session_key="origin-1", request_id="rid-1")
    guard.release_expansion_concurrency_slot(origin_session_key="", request_id="rid-1")
    # The slot is still held.
    decision = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-1", request_id="rid-2"
    )
    assert decision.blocked is True


def test_release_unheld_slot_is_noop() -> None:
    """Releasing an empty slot is silent."""
    # Just exercise the no-op path; nothing to assert beyond "no exception".
    guard.release_expansion_concurrency_slot(
        origin_session_key="origin-never-acquired", request_id="rid-1"
    )


# ---------------------------------------------------------------------------
# record_expansion_delegation_telemetry
# ---------------------------------------------------------------------------


def test_telemetry_increments_counter_per_event() -> None:
    """Acceptance criteria: each call increments the right counter."""
    snapshot_before = guard.get_expansion_delegation_telemetry_snapshot_for_tests()
    assert snapshot_before == {"start": 0, "block": 0, "timeout": 0, "success": 0}

    guard.record_expansion_delegation_telemetry(
        component="lcm_expand_query",
        event="start",
        request_id="rid-1",
        expansion_depth=0,
        origin_session_key="origin",
    )
    guard.record_expansion_delegation_telemetry(
        component="lcm_expand_query",
        event="start",
        request_id="rid-2",
        expansion_depth=0,
        origin_session_key="origin",
    )
    guard.record_expansion_delegation_telemetry(
        component="lcm_expand_query",
        event="block",
        request_id="rid-3",
        expansion_depth=1,
        origin_session_key="origin",
        reason="depth_cap",
    )

    snapshot_after = guard.get_expansion_delegation_telemetry_snapshot_for_tests()
    assert snapshot_after == {"start": 2, "block": 1, "timeout": 0, "success": 0}


def test_telemetry_reset_for_tests_zeros_counters() -> None:
    """Acceptance criteria: ``reset_for_tests`` zeroes the counters."""
    guard.record_expansion_delegation_telemetry(
        component="x",
        event="success",
        request_id="rid",
        expansion_depth=0,
        origin_session_key="origin",
    )
    assert guard.get_expansion_delegation_telemetry_snapshot_for_tests()["success"] == 1
    guard.reset_for_tests()
    assert guard.get_expansion_delegation_telemetry_snapshot_for_tests() == {
        "start": 0,
        "block": 0,
        "timeout": 0,
        "success": 0,
    }


def test_telemetry_start_event_logs_at_info(caplog: pytest.LogCaptureFixture) -> None:
    """``start`` and ``success`` events log at INFO level."""
    caplog.set_level(logging.INFO, logger="lcm.expansion")
    guard.record_expansion_delegation_telemetry(
        component="lcm_expand_query",
        event="start",
        request_id="rid-1",
        expansion_depth=0,
        origin_session_key="origin",
    )
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert info_records, "expected an INFO-level record for 'start' event"
    assert "[lcm][expansion_delegation]" in info_records[-1].getMessage()


def test_telemetry_block_event_logs_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``block`` and ``timeout`` events log at WARNING level."""
    caplog.set_level(logging.WARNING, logger="lcm.expansion")
    guard.record_expansion_delegation_telemetry(
        component="lcm_expand_query",
        event="block",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="origin",
        reason="depth_cap",
    )
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "expected a WARNING-level record for 'block' event"
    msg = warning_records[-1].getMessage()
    assert "[lcm][expansion_delegation]" in msg
    assert "depth_cap" in msg


def test_telemetry_accepts_custom_logger(caplog: pytest.LogCaptureFixture) -> None:
    """The optional ``logger`` kwarg overrides the module-level default."""
    custom = logging.getLogger("test.custom.logger")
    caplog.set_level(logging.INFO, logger="test.custom.logger")
    guard.record_expansion_delegation_telemetry(
        component="lcm_expand_query",
        event="success",
        request_id="rid-1",
        expansion_depth=0,
        origin_session_key="origin",
        logger=custom,
    )
    records = [r for r in caplog.records if r.name == "test.custom.logger"]
    assert records, "expected the custom logger to emit a record"


def test_telemetry_payload_includes_optional_fields() -> None:
    """``reason`` and ``run_id`` make it into the emitted JSON."""
    custom = logging.getLogger("test.telemetry.payload")
    custom.setLevel(logging.INFO)
    records: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
    custom.addHandler(handler)
    try:
        guard.record_expansion_delegation_telemetry(
            component="lcm_expand_query",
            event="block",
            request_id="rid-1",
            expansion_depth=1,
            origin_session_key="origin",
            session_key="child",
            reason="depth_cap",
            run_id="run-42",
            logger=custom,
        )
    finally:
        custom.removeHandler(handler)

    assert records, "expected a log record to be captured"
    line = records[-1]
    assert "depth_cap" in line
    assert "run-42" in line
    assert "child" in line


# ---------------------------------------------------------------------------
# reset_for_tests
# ---------------------------------------------------------------------------


def test_reset_clears_all_state() -> None:
    """``reset_for_tests`` empties every state map."""
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-1",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    guard.acquire_expansion_concurrency_slot(origin_session_key="origin-1", request_id="rid-1")
    guard.evaluate_expansion_recursion_guard(session_key="foo", request_id="rid-blocked")
    guard.record_expansion_delegation_telemetry(
        component="x",
        event="start",
        request_id="rid-1",
        expansion_depth=0,
        origin_session_key="origin-1",
    )

    guard.reset_for_tests()

    assert guard.get_delegated_expansion_context_for_tests("foo") is None
    assert guard.get_expansion_delegation_telemetry_snapshot_for_tests() == {
        "start": 0,
        "block": 0,
        "timeout": 0,
        "success": 0,
    }
    # Slot is now free — a fresh acquire from the same origin succeeds.
    decision = guard.acquire_expansion_concurrency_slot(
        origin_session_key="origin-1", request_id="rid-2"
    )
    assert decision.blocked is False
    # No blocked-id history — first evaluation against a fresh stamp is
    # depth_cap, not idempotent_reentry.
    guard.stamp_delegated_expansion_context(
        session_key="foo",
        request_id="rid-3",
        expansion_depth=1,
        origin_session_key="origin",
        stamped_by="test",
    )
    decision2 = guard.evaluate_expansion_recursion_guard(
        session_key="foo", request_id="rid-blocked"
    )
    assert decision2.blocked is True
    assert decision2.reason == "depth_cap"


# ---------------------------------------------------------------------------
# Concurrency stress
# ---------------------------------------------------------------------------


def test_concurrency_stress_50_threads_exactly_one_wins() -> None:
    """Acceptance criteria: 50 threads acquire same origin; exactly 1 wins."""
    thread_count = 50
    start_barrier = threading.Barrier(thread_count)
    decisions: list[guard.ExpansionConcurrencyGuardDecision] = []
    decisions_lock = threading.Lock()

    def worker(rid: int) -> None:
        start_barrier.wait()  # release all threads simultaneously
        decision = guard.acquire_expansion_concurrency_slot(
            origin_session_key="shared-origin",
            request_id=f"rid-{rid}",
        )
        with decisions_lock:
            decisions.append(decision)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = [d for d in decisions if d.blocked is False]
    blocked = [d for d in decisions if d.blocked is True]
    assert len(allowed) == 1, (
        f"expected exactly 1 winner, got {len(allowed)} (blocked={len(blocked)})"
    )
    assert len(blocked) == thread_count - 1
    for d in blocked:
        assert d.reason == "origin_session_in_flight"


def test_concurrency_stress_stamp_does_not_corrupt_state() -> None:
    """Many parallel stamps on the same session_key leave a coherent context."""
    thread_count = 50
    start_barrier = threading.Barrier(thread_count)

    def worker(rid: int) -> None:
        start_barrier.wait()
        guard.stamp_delegated_expansion_context(
            session_key="shared",
            request_id=f"rid-{rid}",
            expansion_depth=1,
            origin_session_key=f"origin-{rid}",
            stamped_by="stress",
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one rid+origin pair survives — whichever stamped last.
    ctx = guard.get_delegated_expansion_context_for_tests("shared")
    assert ctx is not None
    assert ctx.request_id.startswith("rid-")
    assert ctx.origin_session_key.startswith("origin-")
    # The stamped depth is correctly retained (no torn writes).
    assert ctx.expansion_depth == 1


def test_concurrency_stress_telemetry_counters_monotonic() -> None:
    """Parallel telemetry calls produce exactly thread_count increments."""
    thread_count = 50
    start_barrier = threading.Barrier(thread_count)

    def worker(rid: int) -> None:
        start_barrier.wait()
        guard.record_expansion_delegation_telemetry(
            component="stress",
            event="start",
            request_id=f"rid-{rid}",
            expansion_depth=0,
            origin_session_key="origin",
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snapshot = guard.get_expansion_delegation_telemetry_snapshot_for_tests()
    assert snapshot["start"] == thread_count
    assert snapshot["block"] == 0
    assert snapshot["timeout"] == 0
    assert snapshot["success"] == 0


# ---------------------------------------------------------------------------
# Package re-exports
# ---------------------------------------------------------------------------


def test_re_exports_from_tools_package() -> None:
    """Public API is re-exported via ``lossless_hermes.tools``."""
    from lossless_hermes import tools

    assert tools.EXPANSION_DELEGATION_DEPTH_CAP == 1
    assert tools.EXPANSION_RECURSION_ERROR_CODE == "EXPANSION_RECURSION_BLOCKED"
    assert tools.EXPANSION_CONCURRENCY_ERROR_CODE == "EXPANSION_CONCURRENCY_BLOCKED"
    assert callable(tools.acquire_expansion_concurrency_slot)
    assert callable(tools.evaluate_expansion_recursion_guard)
    assert callable(tools.stamp_delegated_expansion_context)
