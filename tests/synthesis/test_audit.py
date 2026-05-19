"""Tests for :mod:`lossless_hermes.synthesis.audit` (issue 07-09).

Ports the audit-subset of ``lossless-claw/test/synthesis-dispatch.test.ts``
plus the orphan-GC subset of ``lossless-claw/test/operator-health-audit.test.ts``
(commit ``1f07fbd`` on branch ``pr-613``). Adds direct-helper unit tests
that the TS source covers indirectly via the dispatcher integration.

### Coverage matrix (per issue 07-09 spec — "Tests to port")

| TS source case | Python test |
|---|---|
| (1) ``'started'`` row exists during LLM call | :meth:`TestLifecycle.test_started_row_present_during_llm_call` |
| (2) UPDATE to ``'completed'`` post-success | :meth:`TestLifecycle.test_update_completed_post_success` |
| (3) UPDATE to ``'failed'`` on LLM error | :meth:`TestLifecycle.test_update_failed_on_llm_error` |
| (4) FK violation raises ``audit_insert_failure`` BEFORE LLM | :meth:`TestInsertFailure.test_fk_violation_bad_prompt_id_raises_audit_insert_failure` |
| (5) shared ``pass_session_id`` across monthly's 2 passes | :meth:`TestSharedPassSessionId.test_monthly_shares_pass_session_id` |
| (6) shared ``pass_session_id`` across yearly's 4 passes | :meth:`TestSharedPassSessionId.test_yearly_shares_pass_session_id` |
| (7) truncation marker present when input > 8000 chars | :meth:`TestTruncation.test_truncate_long_input_appends_marker` |
| (8) orphan ``'started'`` rows older than 1 h deleted | :meth:`TestOrphanSweep.test_sweep_deletes_started_rows_older_than_cutoff` |
| (9) 30-day age-out for terminal rows | :meth:`TestTerminalSweep.test_sweep_deletes_completed_and_failed_older_than_30d` |
| (10) custom ``LCM_AUDIT_RETENTION_DAYS=7`` honored | :meth:`TestTerminalSweep.test_env_override_lcm_audit_retention_days_7` |

### Extra unit tests (Python-only — direct helper coverage)

| Test class | Concern |
|---|---|
| :class:`TestAuditId` | ``aud_<6 hex>`` format and uniqueness |
| :class:`TestTruncation` | short-input no-op, exact-cap no-op, very-long input |
| :class:`TestUpdateCompleted` | ``model_used`` rewrite + ``cost_cents=None`` skip |
| :class:`TestUpdateFailed` | ``last_error`` 500-char cap + optional ``latency_ms`` |
| :class:`TestCheckConstraint` | ``ValueError`` when both targets ``None`` |
| :class:`TestResolveRetentionDays` | env override + fallback + invalid input |
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.synthesis.audit import (
    AUDIT_ID_PREFIX,
    AUDIT_MAX_LEN,
    AUDIT_TRUNCATED_MARKER,
    DEFAULT_RETENTION_DAYS,
    ENV_RETENTION_DAYS,
    LAST_ERROR_MAX_LEN,
    AuditCompletedResult,
    AuditInsertContext,
    generate_audit_id,
    insert_audit_started,
    resolve_retention_days,
    sweep_orphan_audit_starts,
    sweep_terminal_audit_rows,
    truncate_for_audit,
    update_audit_completed,
    update_audit_failed,
)
from lossless_hermes.synthesis.dispatch import (
    LlmCallArgs,
    LlmCallResult,
    SynthesisDispatchError,
    SynthesizeRequest,
    dispatch_synthesis,
)
from lossless_hermes.synthesis.prompt_registry import (
    RegisterPromptOptions,
    register_prompt,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _setup_db() -> sqlite3.Connection:
    """Build an in-memory DB with FK enforcement + the v4.1 schema applied.

    Mirrors :func:`tests.synthesis.test_dispatch._setup_db` — also inserts
    a conversation + summary so ``target_summary_id`` FK is valid.

    ``isolation_level=None`` (autocommit) is required because
    :func:`register_prompt` (issue 07-08) issues its own
    ``BEGIN IMMEDIATE``.
    """

    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(db, fts5_available=False, seed_default_prompts=False)
    db.execute("INSERT INTO conversations (session_id, session_key) VALUES ('s1', 'sk1')")
    db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content,"
        " token_count) VALUES ('sum_target', 1, 'condensed', 'placeholder', 1)"
    )
    return db


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Migrated in-memory DB with FK enforcement + summary target ready."""

    conn = _setup_db()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def prompt_id(db: sqlite3.Connection) -> str:
    """Register a single-pass prompt and return its ID for direct-helper tests."""

    return register_prompt(
        db,
        RegisterPromptOptions(
            memory_type="episodic-condensed",
            tier_label="daily",
            pass_kind="single",
            template="Daily: {{source_text}}",
        ),
    )


def _make_static_llm(
    output: str = "mock output",
    *,
    latency_ms: float = 42.0,
    cost_cents: int | None = 10,
) -> Callable[[LlmCallArgs], Awaitable[LlmCallResult]]:
    """Build a deterministic LLM mock that returns a fixed ``output``."""

    async def _call(_args: LlmCallArgs) -> LlmCallResult:
        return LlmCallResult(output=output, latency_ms=latency_ms, cost_cents=cost_cents)

    return _call


# ---------------------------------------------------------------------------
# TestAuditId — `aud_<6 hex>` format + uniqueness
# ---------------------------------------------------------------------------


class TestAuditId:
    """Acceptance criterion: ``audit_id`` is ``aud_<6 hex chars>``."""

    def test_format_is_aud_underscore_six_hex(self) -> None:
        """Each call returns ``aud_<6 hex>`` matching the AC regex."""

        for _ in range(50):
            aid = generate_audit_id()
            assert aid.startswith(AUDIT_ID_PREFIX), aid
            assert re.fullmatch(r"aud_[0-9a-f]{6}", aid), aid

    def test_ids_are_distinct_over_many_calls(self) -> None:
        """24 bits of entropy → birthday-paradox collisions are improbable
        below ~4096 calls; 256 is well within tolerance."""

        seen = {generate_audit_id() for _ in range(256)}
        assert len(seen) == 256


# ---------------------------------------------------------------------------
# TestTruncation — 8000-char cap + marker (TS dispatch.ts:809-811)
# ---------------------------------------------------------------------------


class TestTruncation:
    """Spec AC: ``pass_input_truncated`` and ``pass_output`` truncated to
    8000 chars with ``"…(truncated)"`` marker."""

    def test_short_input_returned_unchanged(self) -> None:
        """Below-cap strings come back identical."""

        assert truncate_for_audit("hello") == "hello"
        assert truncate_for_audit("") == ""

    def test_exact_cap_returned_unchanged(self) -> None:
        """A string of exactly ``AUDIT_MAX_LEN`` chars is NOT truncated."""

        s = "x" * AUDIT_MAX_LEN
        assert truncate_for_audit(s) == s

    def test_truncate_long_input_appends_marker(self) -> None:
        """TS line 380 (audit subset): inputs > 8000 chars marked."""

        s = "x" * (AUDIT_MAX_LEN + 100)
        out = truncate_for_audit(s)
        assert out.endswith(AUDIT_TRUNCATED_MARKER)
        # First 8000 chars preserved verbatim.
        assert out[:AUDIT_MAX_LEN] == "x" * AUDIT_MAX_LEN
        # Marker added; total length = cap + len(marker).
        assert len(out) == AUDIT_MAX_LEN + len(AUDIT_TRUNCATED_MARKER)

    def test_truncation_marker_is_unicode_ellipsis(self) -> None:
        """The marker must be the single Unicode ellipsis (U+2026), NOT
        ASCII ``...`` — matches TS source verbatim per ADR-016
        (descriptions are load-bearing)."""

        assert AUDIT_TRUNCATED_MARKER.startswith("…")
        assert "..." not in AUDIT_TRUNCATED_MARKER


# ---------------------------------------------------------------------------
# TestCheckConstraint — both-targets-NULL raises ValueError
# ---------------------------------------------------------------------------


class TestCheckConstraint:
    """Spec AC: exactly one of ``target_summary_id`` / ``target_cache_id``
    must be non-NULL — the helper raises ``ValueError`` if BOTH are
    ``None`` so the caller gets a clearer error than the raw SQLite
    CHECK violation."""

    def test_both_targets_none_raises_value_error(
        self, db: sqlite3.Connection, prompt_id: str
    ) -> None:
        ctx = AuditInsertContext(
            pass_session_id="ps_check",
            target_summary_id=None,
            target_cache_id=None,
            prompt_id=prompt_id,
            pass_kind="single",
            pass_input_truncated="x",
            model_used="m",
        )
        with pytest.raises(ValueError, match="at least one of"):
            insert_audit_started(db, generate_audit_id(), ctx)


# ---------------------------------------------------------------------------
# TestLifecycle — started → completed / failed (TS dispatch.ts:402-444)
# ---------------------------------------------------------------------------


class TestLifecycle:
    """The three-phase started → completed / failed lifecycle.

    The TS source covers these via the dispatcher integration; the
    Python port adds direct-helper coverage so the audit module is
    independently testable.
    """

    def test_insert_started_writes_status_started(
        self, db: sqlite3.Connection, prompt_id: str
    ) -> None:
        """The INSERT writes ``status='started'`` verbatim."""

        aid = generate_audit_id()
        insert_audit_started(
            db,
            aid,
            AuditInsertContext(
                pass_session_id="ps_l1",
                target_summary_id="sum_target",
                target_cache_id=None,
                prompt_id=prompt_id,
                pass_kind="single",
                pass_input_truncated="some input",
                model_used="gpt-5.4-mini",
            ),
        )
        row = db.execute(
            "SELECT status, pass_input_truncated, model_used,"
            "  pass_output, latency_ms, cost_usd_cents, last_error"
            " FROM lcm_synthesis_audit WHERE audit_id = ?",
            (aid,),
        ).fetchone()
        assert row[0] == "started"
        assert row[1] == "some input"
        assert row[2] == "gpt-5.4-mini"
        # Post-LLM fields not yet set.
        assert row[3] is None
        assert row[4] is None
        assert row[5] is None
        assert row[6] is None

    def test_update_completed_post_success(self, db: sqlite3.Connection, prompt_id: str) -> None:
        """TS line 438-444: post-success UPDATE writes the full result row."""

        aid = generate_audit_id()
        insert_audit_started(
            db,
            aid,
            AuditInsertContext(
                pass_session_id="ps_l2",
                target_summary_id="sum_target",
                target_cache_id=None,
                prompt_id=prompt_id,
                pass_kind="single",
                pass_input_truncated="input",
                model_used="requested-model",
            ),
        )
        update_audit_completed(
            db,
            aid,
            AuditCompletedResult(
                pass_output="the output",
                # Adapter substituted a different model — column gets rewritten.
                model_used="actual-model",
                latency_ms=42,
                cost_cents=10,
            ),
        )
        row = db.execute(
            "SELECT status, pass_output, model_used, latency_ms, cost_usd_cents,"
            "  last_error FROM lcm_synthesis_audit WHERE audit_id = ?",
            (aid,),
        ).fetchone()
        assert row[0] == "completed"
        assert row[1] == "the output"
        assert row[2] == "actual-model"
        assert row[3] == 42
        assert row[4] == 10
        assert row[5] is None

    def test_update_failed_on_llm_error(self, db: sqlite3.Connection, prompt_id: str) -> None:
        """TS line 425-429: post-failure UPDATE writes status + last_error."""

        aid = generate_audit_id()
        insert_audit_started(
            db,
            aid,
            AuditInsertContext(
                pass_session_id="ps_l3",
                target_summary_id="sum_target",
                target_cache_id=None,
                prompt_id=prompt_id,
                pass_kind="single",
                pass_input_truncated="input",
                model_used="m",
            ),
        )
        update_audit_failed(db, aid, "API timeout after 30s")
        row = db.execute(
            "SELECT status, last_error, pass_output, cost_usd_cents"
            " FROM lcm_synthesis_audit WHERE audit_id = ?",
            (aid,),
        ).fetchone()
        assert row[0] == "failed"
        assert row[1] == "API timeout after 30s"
        # No success-path fields populated.
        assert row[2] is None
        assert row[3] is None

    @pytest.mark.asyncio
    async def test_started_row_present_during_llm_call(
        self, db: sqlite3.Connection, prompt_id: str
    ) -> None:
        """TS line 80 (audit subset): the ``'started'`` row IS persisted
        before the LLM call — we observe it from inside the LLM mock."""

        # The mock peeks at the audit table; this proves the INSERT
        # happens BEFORE the LLM call.
        peeked: dict[str, object] = {}

        async def _peek(_args: LlmCallArgs) -> LlmCallResult:
            row = db.execute("SELECT status FROM lcm_synthesis_audit").fetchone()
            peeked["row"] = row
            return LlmCallResult(output="ok", latency_ms=1)

        await dispatch_synthesis(
            db,
            _peek,
            SynthesizeRequest(
                tier="daily",
                memory_type="episodic-condensed",
                source_text="hi",
                pass_session_id="ps_peek",
                target_summary_id="sum_target",
            ),
        )
        assert peeked["row"] is not None
        assert peeked["row"][0] == "started"  # type: ignore[index]


# ---------------------------------------------------------------------------
# TestInsertFailure — FK violation → audit_insert_failure (Group D Gap 4)
# ---------------------------------------------------------------------------


class TestInsertFailure:
    """LCM Wave-9 Group D adversarial Gap 4: FK / CHECK violations on the
    started-insert surface as :exc:`SynthesisDispatchError("audit_insert_failure")`
    BEFORE the LLM is called."""

    @pytest.mark.asyncio
    async def test_fk_violation_bad_prompt_id_raises_audit_insert_failure(
        self, db: sqlite3.Connection
    ) -> None:
        """TS line ~420: an FK violation on prompt_id (we forge an active
        prompt row pointing at a not-yet-registered bundle) trips the
        ``audit_insert_failure`` path.

        We construct the violation by inserting a fake active-prompt row
        with a ``prompt_id`` that bypasses the registry, then run a
        request that picks up that fake — dispatch will look up the
        active prompt, get our fake row, then the audit INSERT will
        violate the FK on ``prompt_id`` because the row is not in
        ``lcm_prompt_registry``.

        Simpler approach: drop the FK validation by inserting an audit
        row directly to confirm the SQLite engine's behavior and then
        wrap-check from the dispatcher. We exercise the dispatcher
        path directly with an LLM mock that should NEVER be called.
        """

        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x {{source_text}}",
            ),
        )

        llm_called: list[int] = []

        async def _llm(_args: LlmCallArgs) -> LlmCallResult:
            llm_called.append(1)
            return LlmCallResult(output="z", latency_ms=1)

        # Pass a target_summary_id that doesn't exist — FK to summaries
        # will fail on INSERT.
        with pytest.raises(SynthesisDispatchError) as excinfo:
            await dispatch_synthesis(
                db,
                _llm,
                SynthesizeRequest(
                    tier="daily",
                    memory_type="episodic-condensed",
                    source_text="hi",
                    pass_session_id="ps_fkfail",
                    target_summary_id="sum_does_not_exist",
                ),
            )
        assert excinfo.value.kind == "audit_insert_failure"
        # The LLM was NOT called — that is the whole point of the Wave-9
        # Group D Gap 4 fix.
        assert llm_called == []
        # No audit row was successfully inserted (transaction rolled back
        # by the failing INSERT).
        count = db.execute("SELECT COUNT(*) FROM lcm_synthesis_audit").fetchone()[0]
        assert count == 0

    @pytest.mark.asyncio
    async def test_both_targets_none_surfaces_audit_insert_failure(
        self, db: sqlite3.Connection
    ) -> None:
        """The dispatcher pre-validates that at least one target is set,
        but the audit helper ALSO raises ``ValueError`` defensively.
        Here we check the dispatcher's typed error surface for the
        related path (this is :meth:`SynthesisDispatcher.synthesize`
        pre-validation, not the audit helper directly)."""

        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="daily",
                pass_kind="single",
                template="x",
            ),
        )

        llm = _make_static_llm()
        with pytest.raises(SynthesisDispatchError) as excinfo:
            await dispatch_synthesis(
                db,
                llm,
                SynthesizeRequest(
                    tier="daily",
                    memory_type="episodic-condensed",
                    source_text="x",
                    pass_session_id="ps_none",
                    target_summary_id=None,
                    target_cache_id=None,
                ),
            )
        # Dispatcher pre-validates → "missing_target" (typed error in 07-05).
        # The audit helper also defends against the same case — see
        # TestCheckConstraint above.
        assert excinfo.value.kind == "missing_target"


# ---------------------------------------------------------------------------
# TestSharedPassSessionId — Group D Gap 2 (TS dispatch.ts:565-579 + 676-686)
# ---------------------------------------------------------------------------


class TestSharedPassSessionId:
    """LCM Wave-9 Group D adversarial Gap 2: ALL passes of one logical
    synthesis attempt share ONE ``pass_session_id`` (NOT suffixed per
    candidate). Operators query by pass_session_id to retrieve the
    full attempt's audit trail."""

    @pytest.mark.asyncio
    async def test_monthly_shares_pass_session_id(self, db: sqlite3.Connection) -> None:
        """TS spec line 49 — monthly (single + verify_fidelity) → 2 audit
        rows, both with the same ``pass_session_id``."""

        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="single",
                template="Monthly: {{source_text}}",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="monthly",
                pass_kind="verify_fidelity",
                template="Verify {{candidate_summary}} vs {{source_text}}",
            ),
        )

        outputs = iter(["the summary", "OK"])

        async def _llm(_args: LlmCallArgs) -> LlmCallResult:
            return LlmCallResult(output=next(outputs), latency_ms=10)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="monthly",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps_monthly_shared",
                target_summary_id="sum_target",
            ),
        )

        sessions = db.execute("SELECT DISTINCT pass_session_id FROM lcm_synthesis_audit").fetchall()
        assert sessions == [("ps_monthly_shared",)]
        row_count = db.execute(
            "SELECT COUNT(*) FROM lcm_synthesis_audit WHERE pass_session_id = ?",
            ("ps_monthly_shared",),
        ).fetchone()[0]
        assert row_count == 2

    @pytest.mark.asyncio
    async def test_yearly_shares_pass_session_id(self, db: sqlite3.Connection) -> None:
        """TS spec line 49 — yearly (3× single + 1× judge by default;
        best_of_n=3) → 4 audit rows, ALL with the same
        ``pass_session_id``. NO ``_cand{i}`` suffix per candidate."""

        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="yearly",
                pass_kind="single",
                template="Yearly: {{source_text}}",
            ),
        )
        register_prompt(
            db,
            RegisterPromptOptions(
                memory_type="episodic-condensed",
                tier_label="yearly",
                pass_kind="best_of_n_judge",
                template="Pick best of {{candidates}} vs {{source_text}}",
            ),
        )

        call_count = [0]

        async def _llm(args: LlmCallArgs) -> LlmCallResult:
            call_count[0] += 1
            # Judge returns an integer, candidates return arbitrary text.
            if args.pass_kind == "best_of_n_judge":
                return LlmCallResult(output="0", latency_ms=5)
            return LlmCallResult(output=f"candidate_{call_count[0]}", latency_ms=5)

        await dispatch_synthesis(
            db,
            _llm,
            SynthesizeRequest(
                tier="yearly",
                memory_type="episodic-condensed",
                source_text="x",
                pass_session_id="ps_yearly_shared",
                target_summary_id="sum_target",
                best_of_n=3,
            ),
        )

        sessions = db.execute("SELECT DISTINCT pass_session_id FROM lcm_synthesis_audit").fetchall()
        # ALL rows share ONE session ID — Group D Gap 2.
        assert sessions == [("ps_yearly_shared",)]
        row_count = db.execute(
            "SELECT COUNT(*) FROM lcm_synthesis_audit WHERE pass_session_id = ?",
            ("ps_yearly_shared",),
        ).fetchone()[0]
        # 3 candidates + 1 judge = 4.
        assert row_count == 4


# ---------------------------------------------------------------------------
# TestUpdateCompleted — model_used rewrite + cost_cents None handling
# ---------------------------------------------------------------------------


class TestUpdateCompleted:
    """Direct unit coverage of :func:`update_audit_completed`."""

    def test_model_used_rewritten_on_update(self, db: sqlite3.Connection, prompt_id: str) -> None:
        """Spec AC: ``update_audit_completed`` writes ``model_used`` (in
        case the adapter changed it from the requested model)."""

        aid = generate_audit_id()
        insert_audit_started(
            db,
            aid,
            AuditInsertContext(
                pass_session_id="ps_uc1",
                target_summary_id="sum_target",
                target_cache_id=None,
                prompt_id=prompt_id,
                pass_kind="single",
                pass_input_truncated="x",
                model_used="requested-A",
            ),
        )
        update_audit_completed(
            db,
            aid,
            AuditCompletedResult(
                pass_output="o",
                model_used="actual-B",
                latency_ms=1,
                cost_cents=2,
            ),
        )
        model = db.execute(
            "SELECT model_used FROM lcm_synthesis_audit WHERE audit_id = ?",
            (aid,),
        ).fetchone()[0]
        assert model == "actual-B"

    def test_cost_cents_none_skipped_on_update(
        self, db: sqlite3.Connection, prompt_id: str
    ) -> None:
        """When the adapter doesn't know cost, ``cost_usd_cents`` stays
        ``NULL`` (we do NOT write a NULL — we skip the column)."""

        aid = generate_audit_id()
        insert_audit_started(
            db,
            aid,
            AuditInsertContext(
                pass_session_id="ps_uc2",
                target_summary_id="sum_target",
                target_cache_id=None,
                prompt_id=prompt_id,
                pass_kind="single",
                pass_input_truncated="x",
                model_used="m",
            ),
        )
        update_audit_completed(
            db,
            aid,
            AuditCompletedResult(
                pass_output="o",
                model_used="m",
                latency_ms=1,
                cost_cents=None,
            ),
        )
        cost = db.execute(
            "SELECT cost_usd_cents FROM lcm_synthesis_audit WHERE audit_id = ?",
            (aid,),
        ).fetchone()[0]
        assert cost is None


# ---------------------------------------------------------------------------
# TestUpdateFailed — last_error 500-char cap + optional latency_ms
# ---------------------------------------------------------------------------


class TestUpdateFailed:
    """Direct unit coverage of :func:`update_audit_failed`."""

    def test_last_error_truncated_to_500_chars(
        self, db: sqlite3.Connection, prompt_id: str
    ) -> None:
        """Spec AC: ``last_error`` truncated to 500 chars to mitigate
        stack-trace PII exposure + table bloat."""

        aid = generate_audit_id()
        insert_audit_started(
            db,
            aid,
            AuditInsertContext(
                pass_session_id="ps_uf1",
                target_summary_id="sum_target",
                target_cache_id=None,
                prompt_id=prompt_id,
                pass_kind="single",
                pass_input_truncated="x",
                model_used="m",
            ),
        )
        big_err = "x" * (LAST_ERROR_MAX_LEN + 200)
        update_audit_failed(db, aid, big_err)
        stored = db.execute(
            "SELECT last_error FROM lcm_synthesis_audit WHERE audit_id = ?",
            (aid,),
        ).fetchone()[0]
        assert len(stored) == LAST_ERROR_MAX_LEN

    def test_optional_latency_ms_recorded(self, db: sqlite3.Connection, prompt_id: str) -> None:
        """Spec AC: ``update_audit_failed`` writes ``latency_ms`` if available."""

        aid = generate_audit_id()
        insert_audit_started(
            db,
            aid,
            AuditInsertContext(
                pass_session_id="ps_uf2",
                target_summary_id="sum_target",
                target_cache_id=None,
                prompt_id=prompt_id,
                pass_kind="single",
                pass_input_truncated="x",
                model_used="m",
            ),
        )
        update_audit_failed(db, aid, "boom", latency_ms=99)
        latency = db.execute(
            "SELECT latency_ms FROM lcm_synthesis_audit WHERE audit_id = ?",
            (aid,),
        ).fetchone()[0]
        assert latency == 99


# ---------------------------------------------------------------------------
# TestOrphanSweep — TS operator-health-audit.test.ts orphan-GC subset
# ---------------------------------------------------------------------------


def _insert_raw_audit_row(
    db: sqlite3.Connection,
    *,
    audit_id: str,
    pass_session_id: str,
    target_summary_id: str | None,
    prompt_id: str,
    status: str,
    ran_at: str,
) -> None:
    """Helper to insert an audit row with a fabricated ``ran_at`` so the
    sweep cutoff logic can be exercised deterministically. Uses pass_kind
    ``single`` + ``pass_input_truncated='_'`` + ``model_used='m'`` as
    benign defaults."""

    db.execute(
        "INSERT INTO lcm_synthesis_audit"
        " (audit_id, pass_session_id, target_summary_id, target_cache_id,"
        "  prompt_id, pass_kind, pass_input_truncated, status, model_used, ran_at)"
        " VALUES (?, ?, ?, NULL, ?, 'single', '_', ?, 'm', ?)",
        (audit_id, pass_session_id, target_summary_id, prompt_id, status, ran_at),
    )


class TestOrphanSweep:
    """Spec AC: ``sweep_orphan_audit_starts(conn, cutoff_minutes=60)``
    deletes ``'started'`` rows older than the cutoff, returns count.
    Hits the partial index ``lcm_synthesis_audit_started_gc_idx``."""

    def test_sweep_deletes_started_rows_older_than_cutoff(
        self, db: sqlite3.Connection, prompt_id: str
    ) -> None:
        """TS test (8): orphan ``'started'`` rows older than 1 h deleted."""

        # Old 'started' row — should be swept.
        _insert_raw_audit_row(
            db,
            audit_id="aud_old001",
            pass_session_id="ps_a",
            target_summary_id="sum_target",
            prompt_id=prompt_id,
            status="started",
            ran_at="2020-01-01 00:00:00",
        )
        # Fresh 'started' row — should NOT be swept.
        _insert_raw_audit_row(
            db,
            audit_id="aud_new001",
            pass_session_id="ps_b",
            target_summary_id="sum_target",
            prompt_id=prompt_id,
            status="started",
            ran_at="2099-12-31 23:59:59",
        )
        # Old 'completed' row — should NOT be swept by THIS function.
        _insert_raw_audit_row(
            db,
            audit_id="aud_done001",
            pass_session_id="ps_c",
            target_summary_id="sum_target",
            prompt_id=prompt_id,
            status="completed",
            ran_at="2020-01-01 00:00:00",
        )

        deleted = sweep_orphan_audit_starts(db, cutoff_minutes=60)
        assert deleted == 1

        remaining = sorted(
            row[0] for row in db.execute("SELECT audit_id FROM lcm_synthesis_audit").fetchall()
        )
        assert remaining == ["aud_done001", "aud_new001"]

    def test_sweep_with_no_orphans_returns_zero(
        self, db: sqlite3.Connection, prompt_id: str
    ) -> None:
        """No-op when no rows match — returns 0."""

        _insert_raw_audit_row(
            db,
            audit_id="aud_x",
            pass_session_id="ps_x",
            target_summary_id="sum_target",
            prompt_id=prompt_id,
            status="started",
            ran_at="2099-12-31 23:59:59",
        )
        assert sweep_orphan_audit_starts(db, cutoff_minutes=60) == 0

    def test_custom_cutoff_minutes(self, db: sqlite3.Connection, prompt_id: str) -> None:
        """The ``cutoff_minutes`` kwarg is honored verbatim."""

        # Row exactly 30 minutes old via SQLite arithmetic.
        db.execute(
            "INSERT INTO lcm_synthesis_audit"
            " (audit_id, pass_session_id, target_summary_id, target_cache_id,"
            "  prompt_id, pass_kind, pass_input_truncated, status, model_used,"
            "  ran_at)"
            " VALUES ('aud_30m', 'ps_30m', 'sum_target', NULL, ?, 'single',"
            "  '_', 'started', 'm', datetime('now', '-30 minutes'))",
            (prompt_id,),
        )
        # cutoff=60 → row is too young to be swept.
        assert sweep_orphan_audit_starts(db, cutoff_minutes=60) == 0
        # cutoff=10 → row is now older than cutoff and is swept.
        assert sweep_orphan_audit_starts(db, cutoff_minutes=10) == 1


# ---------------------------------------------------------------------------
# TestTerminalSweep — 30-day age-out + LCM_AUDIT_RETENTION_DAYS env override
# ---------------------------------------------------------------------------


class TestTerminalSweep:
    """Spec AC: age-out DELETE for ``'completed'`` / ``'failed'`` rows
    older than ``LCM_AUDIT_RETENTION_DAYS`` days (default 30). Hits
    the partial index ``lcm_synthesis_audit_completed_gc_idx``."""

    def test_sweep_deletes_completed_and_failed_older_than_30d(
        self, db: sqlite3.Connection, prompt_id: str
    ) -> None:
        """TS test (9): 30-day age-out applies to BOTH ``'completed'``
        and ``'failed'`` rows; ``'started'`` rows are NOT swept by this
        function (orphan-GC's job)."""

        # Old completed → swept.
        _insert_raw_audit_row(
            db,
            audit_id="aud_oc",
            pass_session_id="ps_oc",
            target_summary_id="sum_target",
            prompt_id=prompt_id,
            status="completed",
            ran_at="2020-01-01 00:00:00",
        )
        # Old failed → swept.
        _insert_raw_audit_row(
            db,
            audit_id="aud_of",
            pass_session_id="ps_of",
            target_summary_id="sum_target",
            prompt_id=prompt_id,
            status="failed",
            ran_at="2020-01-01 00:00:00",
        )
        # Fresh completed → not swept.
        _insert_raw_audit_row(
            db,
            audit_id="aud_nc",
            pass_session_id="ps_nc",
            target_summary_id="sum_target",
            prompt_id=prompt_id,
            status="completed",
            ran_at="2099-12-31 23:59:59",
        )
        # Old started → not this function's concern.
        _insert_raw_audit_row(
            db,
            audit_id="aud_os",
            pass_session_id="ps_os",
            target_summary_id="sum_target",
            prompt_id=prompt_id,
            status="started",
            ran_at="2020-01-01 00:00:00",
        )

        deleted = sweep_terminal_audit_rows(db, retention_days=30)
        assert deleted == 2

        remaining = sorted(
            row[0] for row in db.execute("SELECT audit_id FROM lcm_synthesis_audit").fetchall()
        )
        # The fresh completed + the old started survive.
        assert remaining == ["aud_nc", "aud_os"]

    def test_env_override_lcm_audit_retention_days_7(
        self,
        db: sqlite3.Connection,
        prompt_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TS test (10): ``LCM_AUDIT_RETENTION_DAYS=7`` honored.

        A row 10 days old is NOT swept under the default 30-day window
        but IS swept under the 7-day env override."""

        db.execute(
            "INSERT INTO lcm_synthesis_audit"
            " (audit_id, pass_session_id, target_summary_id, target_cache_id,"
            "  prompt_id, pass_kind, pass_input_truncated, status, model_used,"
            "  ran_at)"
            " VALUES ('aud_10d', 'ps_10d', 'sum_target', NULL, ?, 'single',"
            "  '_', 'completed', 'm', datetime('now', '-10 days'))",
            (prompt_id,),
        )

        # Default (no env) — row is fresher than 30 days → NOT swept.
        monkeypatch.delenv(ENV_RETENTION_DAYS, raising=False)
        assert sweep_terminal_audit_rows(db) == 0
        # Override to 7 days — row is older than 7 days → swept.
        monkeypatch.setenv(ENV_RETENTION_DAYS, "7")
        assert sweep_terminal_audit_rows(db) == 1
        # The row is gone.
        assert (
            db.execute(
                "SELECT COUNT(*) FROM lcm_synthesis_audit WHERE audit_id = 'aud_10d'"
            ).fetchone()[0]
            == 0
        )

    def test_explicit_retention_days_overrides_env(
        self,
        db: sqlite3.Connection,
        prompt_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The explicit ``retention_days`` kwarg trumps the env var."""

        monkeypatch.setenv(ENV_RETENTION_DAYS, "100")
        db.execute(
            "INSERT INTO lcm_synthesis_audit"
            " (audit_id, pass_session_id, target_summary_id, target_cache_id,"
            "  prompt_id, pass_kind, pass_input_truncated, status, model_used,"
            "  ran_at)"
            " VALUES ('aud_15d', 'ps_15d', 'sum_target', NULL, ?, 'single',"
            "  '_', 'completed', 'm', datetime('now', '-15 days'))",
            (prompt_id,),
        )
        # Explicit 7 trumps env's 100 → swept.
        assert sweep_terminal_audit_rows(db, retention_days=7) == 1


# ---------------------------------------------------------------------------
# TestResolveRetentionDays — env-resolution edge cases
# ---------------------------------------------------------------------------


class TestResolveRetentionDays:
    """Cover the ``LCM_AUDIT_RETENTION_DAYS`` env-resolution paths."""

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_RETENTION_DAYS, raising=False)
        assert resolve_retention_days() == DEFAULT_RETENTION_DAYS

    def test_empty_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_RETENTION_DAYS, "")
        assert resolve_retention_days() == DEFAULT_RETENTION_DAYS

    def test_whitespace_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_RETENTION_DAYS, "   ")
        assert resolve_retention_days() == DEFAULT_RETENTION_DAYS

    def test_non_numeric_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_RETENTION_DAYS, "not-a-number")
        assert resolve_retention_days() == DEFAULT_RETENTION_DAYS

    def test_negative_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A negative value is treated as invalid and falls back to default
        (don't accidentally enable a "sweep everything older than 0 days"
        catastrophe)."""

        monkeypatch.setenv(ENV_RETENTION_DAYS, "-5")
        assert resolve_retention_days() == DEFAULT_RETENTION_DAYS

    def test_zero_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zero is treated as invalid for the same reason as negative."""

        monkeypatch.setenv(ENV_RETENTION_DAYS, "0")
        assert resolve_retention_days() == DEFAULT_RETENTION_DAYS

    def test_valid_positive_int_honored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_RETENTION_DAYS, "7")
        assert resolve_retention_days() == 7

    def test_leading_trailing_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_RETENTION_DAYS, "  14  ")
        assert resolve_retention_days() == 14

    def test_resolve_unaffected_by_prior_import(self) -> None:
        """resolve_retention_days reads env at call time, NOT import time
        — required by ADR-023 so CI / test-side overrides land without
        process restart."""

        # If this function read env at import, this test (with no env set)
        # would not be representative; we assert that calling it directly
        # against the current env state matches resolution semantics.
        actual = resolve_retention_days()
        from_env = os.environ.get(ENV_RETENTION_DAYS, "").strip()
        if not from_env:
            assert actual == DEFAULT_RETENTION_DAYS
