"""Tests for :mod:`lossless_hermes.commands.eval` (issue #143).

Exercises the ``/lcm eval`` slash-command handler that wraps
:func:`lossless_hermes.operator.eval_runner.run_eval`. The runner's own
recall / drift mechanics are covered by ``tests/operator/test_eval_runner.py``;
the tests here pin the command layer:

* argparse for ``--baseline`` / ``--mode`` / ``--query-set`` /
  ``--version``, including required-flag enforcement and the per-flag
  validation errors;
* the async ``run_eval`` bridge (``asyncio.run``) and that the picked
  adapter is the one actually invoked;
* the operator-facing text for the happy path, the missing-query-set
  error path, and the DB-unavailable short-circuit;
* the mode-specific note / warning sections.

See:

* ``epics/08-cli-ops/08-13-eval-runner.md`` — the eval-runner issue.
* ``src/lossless_hermes/commands/eval.py`` — the handler under test.
* ``lossless-claw/src/plugin/lcm-command.ts:282-336, 446-472,
  1965-2118`` — TS source pinned at commit ``1f07fbd``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

import lossless_hermes.commands.eval as eval_mod
from lossless_hermes.commands.eval import run as run_eval_command
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.eval.query_set import QueryRecord, QuerySetIdentity, register_query_set


# ---------------------------------------------------------------------------
# Fixtures + stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakeEngine:
    """Minimal engine stub exposing ``_db``.

    The handler's ``_resolve_db`` probes ``_db`` first (the wired-engine
    canonical attribute), so a stub carrying ``_db`` matches the
    production path.
    """

    _db: sqlite3.Connection | None


@dataclass
class _FakeParsed:
    """Minimal :class:`ParsedLcmCommand`-shaped stub for tests.

    The handler re-tokenizes ``raw_args`` itself (the router's pre-parse
    does not handle ``--mode`` / ``--query-set`` / ``--version``), so the
    only fields that matter are ``raw_args`` and ``engine``.
    """

    raw_args: str
    engine: _FakeEngine
    name: str = "eval"
    tokens: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)


def _new_db() -> sqlite3.Connection:
    """In-memory SQLite with the full LCM migration ladder applied.

    FTS5 is disabled (``fts5_available=False``) so the suite runs on
    Python builds without FTS5 — matches ``tests/operator/test_eval_runner.py``.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    return conn


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = _new_db()
    try:
        yield conn
    finally:
        conn.close()


SAMPLE_QUERIES: tuple[QueryRecord, ...] = (
    QueryRecord(
        query_id="q1",
        query_text="what is the timezone setting",
        stratum="fts-easy",
        expected_summary_ids=("leaf_a", "leaf_b"),
    ),
    QueryRecord(
        query_id="q2",
        query_text="describe the rebase workflow",
        stratum="paraphrastic",
        expected_summary_ids=("leaf_c",),
    ),
)
"""A small registered query set for the runner-invocation tests."""


def _identity() -> QuerySetIdentity:
    return QuerySetIdentity(name="test-set", version=1)


def _parsed(raw_args: str, *, db: sqlite3.Connection | None) -> _FakeParsed:
    """Build a parsed-command stub with the given raw args + DB."""
    return _FakeParsed(raw_args=raw_args, engine=_FakeEngine(_db=db))


class _StubAdapter:
    """Deterministic recall adapter returning canned hits per query id.

    Mirrors the ``_MockAdapter`` in ``tests/operator/test_eval_runner.py``.
    Used as a seam: the handler's adapter builder is monkeypatched to
    return this so the command-layer test never touches a real
    SummaryStore / hybrid pipeline.
    """

    def __init__(self, canned: dict[str, list[str]]) -> None:
        self._canned = canned
        self.call_count = 0

    async def search(self, query: Any) -> list[str]:
        self.call_count += 1
        return list(self._canned.get(query.query_id, []))


# ===========================================================================
# Argument parsing — required-flag enforcement
# ===========================================================================


class TestRequiredFlagEnforcement:
    """``--baseline`` OR ``--mode`` is required (TS lcm-command.ts:457-463)."""

    def test_bare_eval_is_rejected_as_ambiguous(self, db: sqlite3.Connection) -> None:
        """`/lcm eval` with no flags → parse_error naming --baseline/--mode."""
        out = run_eval_command(_parsed("eval", db=db))
        assert "parse_error" in out
        assert "--baseline" in out
        assert "--mode" in out
        # The runner must NOT have been reached — no Result section.
        assert "Result:" not in out

    def test_baseline_flag_alone_is_accepted(self, db: sqlite3.Connection) -> None:
        """`/lcm eval --baseline` parses cleanly (resolves to fts_only).

        With no query set registered the run then fails with a
        missing-query-set error — but the parse itself succeeds, which
        is what this test pins (no ``parse_error`` in the output).
        """
        out = run_eval_command(_parsed("eval --baseline", db=db))
        assert "parse_error" not in out
        # --baseline resolves to fts_only against the default query set.
        assert "mode: `fts_only`" in out
        assert "eva-baseline v1" in out

    def test_mode_flag_alone_is_accepted(self, db: sqlite3.Connection) -> None:
        """`/lcm eval --mode fts_only` parses cleanly without --baseline."""
        out = run_eval_command(_parsed("eval --mode fts_only", db=db))
        assert "parse_error" not in out
        assert "mode: `fts_only`" in out


# ===========================================================================
# Argument parsing — per-flag validation
# ===========================================================================


class TestModeValidation:
    """``--mode`` value validation (TS lcm-command.ts:303-312)."""

    def test_unknown_mode_is_rejected(self, db: sqlite3.Connection) -> None:
        """`--mode bogus` → parse_error listing the valid modes."""
        out = run_eval_command(_parsed("eval --mode bogus", db=db))
        assert "parse_error" in out
        assert "Unknown mode" in out
        assert "fts_only" in out
        assert "semantic_only" in out
        assert "hybrid" in out

    def test_mode_with_no_value_is_rejected(self, db: sqlite3.Connection) -> None:
        """`--mode` at end of input → parse_error (missing value)."""
        out = run_eval_command(_parsed("eval --mode", db=db))
        assert "parse_error" in out
        assert "`--mode` requires a value" in out

    @pytest.mark.parametrize("mode", ["fts_only", "semantic_only", "hybrid"])
    def test_each_valid_mode_parses(self, db: sqlite3.Connection, mode: str) -> None:
        """Every documented mode is accepted by the parser."""
        out = run_eval_command(_parsed(f"eval --mode {mode}", db=db))
        assert "parse_error" not in out
        assert f"mode: `{mode}`" in out


class TestVersionValidation:
    """``--version`` value validation (TS lcm-command.ts:320-329)."""

    def test_version_with_no_value_is_rejected(self, db: sqlite3.Connection) -> None:
        """`--version` at end of input → parse_error (missing value)."""
        out = run_eval_command(_parsed("eval --mode fts_only --version", db=db))
        assert "parse_error" in out
        assert "`--version` requires a value" in out

    def test_non_integer_version_is_rejected(self, db: sqlite3.Connection) -> None:
        """`--version abc` → parse_error (must be a positive integer)."""
        out = run_eval_command(_parsed("eval --mode fts_only --version abc", db=db))
        assert "parse_error" in out
        assert "positive integer" in out

    def test_zero_version_is_rejected(self, db: sqlite3.Connection) -> None:
        """`--version 0` → parse_error (must be >= 1)."""
        out = run_eval_command(_parsed("eval --mode fts_only --version 0", db=db))
        assert "parse_error" in out
        assert "positive integer" in out

    def test_negative_version_is_rejected(self, db: sqlite3.Connection) -> None:
        """`--version -3` → parse_error (must be >= 1)."""
        out = run_eval_command(_parsed("eval --mode fts_only --version -3", db=db))
        assert "parse_error" in out
        assert "positive integer" in out


class TestUnknownArgument:
    """Unknown flags / bare positionals are rejected (TS lcm-command.ts:331)."""

    def test_unknown_flag_is_rejected(self, db: sqlite3.Connection) -> None:
        """`--bogus` → parse_error naming the offending token."""
        out = run_eval_command(_parsed("eval --baseline --bogus", db=db))
        assert "parse_error" in out
        assert "--bogus" in out

    def test_bare_positional_is_rejected(self, db: sqlite3.Connection) -> None:
        """A bare positional arg → parse_error (TS rejects with the same
        generic "Unknown argument" message)."""
        out = run_eval_command(_parsed("eval --baseline stray", db=db))
        assert "parse_error" in out
        assert "stray" in out


class TestQuerySetParsing:
    """``--query-set`` / ``--version`` resolution + defaults."""

    def test_query_set_with_no_value_is_rejected(self, db: sqlite3.Connection) -> None:
        """`--query-set` at end of input → parse_error (missing value)."""
        out = run_eval_command(_parsed("eval --mode fts_only --query-set", db=db))
        assert "parse_error" in out
        assert "`--query-set` requires a value" in out

    def test_defaults_applied_when_query_set_omitted(self, db: sqlite3.Connection) -> None:
        """Omitting `--query-set` / `--version` resolves to eva-baseline v1."""
        out = run_eval_command(_parsed("eval --mode hybrid", db=db))
        assert "query set: eva-baseline v1" in out

    def test_explicit_query_set_and_version_echoed(self, db: sqlite3.Connection) -> None:
        """Explicit `--query-set` / `--version` show in the Plan section."""
        out = run_eval_command(
            _parsed("eval --mode fts_only --query-set wave12 --version 3", db=db)
        )
        assert "query set: wave12 v3" in out

    def test_unbalanced_quote_is_rejected(self, db: sqlite3.Connection) -> None:
        """Unbalanced quote in raw args → parse_error, not a stack trace."""
        out = run_eval_command(_parsed('eval --query-set "missing close', db=db))
        assert "parse_error" in out
        assert "argument parse error" in out


# ===========================================================================
# DB-unavailable short-circuit
# ===========================================================================


def test_db_unavailable_renders_unavailable_block() -> None:
    """No engine DB connection → ``unavailable`` block, no AttributeError."""
    out = run_eval_command(_parsed("eval --baseline", db=None))
    assert out.startswith("[lcm] eval")
    assert "unavailable" in out
    # The Plan section is still rendered (parse succeeded before the DB
    # resolution).
    assert "mode: `fts_only`" in out


# ===========================================================================
# Runner invocation — happy path + error paths
# ===========================================================================


class TestRunnerInvocation:
    """End-to-end: the handler bridges to ``run_eval`` and renders the report."""

    def test_happy_path_renders_recall_report(self, db: sqlite3.Connection) -> None:
        """A registered query set + fts_only mode renders the Result section.

        Uses the real ``fts_only`` adapter against a migrated DB. The
        ``summaries`` table is empty, so recall is 0 — but the run
        completes, writes an ``lcm_eval_run`` row, and renders the
        ``format_eval_report`` block. This exercises the real async
        bridge (``asyncio.run``) end-to-end.
        """
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        out = run_eval_command(
            _parsed("eval --mode fts_only --query-set test-set --version 1", db=db)
        )
        assert out.startswith("[lcm] eval")
        assert "Result:" in out
        # format_eval_report's signature lines.
        assert "Eval run" in out
        assert "Recall@K" in out
        # A run row was actually written.
        run_count = db.execute("SELECT COUNT(*) FROM lcm_eval_run").fetchone()[0]
        assert run_count == 1

    def test_happy_path_via_seam_adapter_invokes_runner(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The handler invokes the picked adapter for every query.

        Monkeypatches ``_build_adapter_for_mode`` to return a
        deterministic stub adapter — the seam that lets the command-layer
        test avoid the real SummaryStore / hybrid pipeline. We assert the
        adapter was called once per query (proving the runner ran) and
        that the recall the stub fed in shows up in the rendered report.
        """
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        stub = _StubAdapter({
            "q1": ["leaf_a", "leaf_b"],  # both expected hit → recall 1.0
            "q2": ["leaf_c"],  # expected at rank 1
        })
        monkeypatch.setattr(eval_mod, "_build_adapter_for_mode", lambda _db, _mode: stub)

        out = run_eval_command(
            _parsed("eval --mode hybrid --query-set test-set --version 1", db=db)
        )
        # Adapter was invoked once per query in the set.
        assert stub.call_count == len(SAMPLE_QUERIES)
        assert "Result:" in out
        # q1 + q2 both scored a perfect hit → overall recall is nonzero.
        row = db.execute("SELECT retrieval_recall_score FROM lcm_eval_run").fetchone()
        assert row[0] > 0

    def test_first_run_reports_no_prior_drift(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first run of a (query_set, mode) renders the new-baseline note."""
        register_query_set(db, _identity(), SAMPLE_QUERIES)
        stub = _StubAdapter({"q1": ["leaf_a"], "q2": ["leaf_c"]})
        monkeypatch.setattr(eval_mod, "_build_adapter_for_mode", lambda _db, _mode: stub)

        out = run_eval_command(
            _parsed("eval --mode fts_only --query-set test-set --version 1", db=db)
        )
        assert "no prior run" in out

    def test_baseline_flag_runs_against_registered_default_set(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--baseline` runs fts_only against the default ``eva-baseline v1`` set."""
        register_query_set(db, QuerySetIdentity(name="eva-baseline", version=1), SAMPLE_QUERIES)
        stub = _StubAdapter({"q1": ["leaf_a"], "q2": ["leaf_c"]})
        monkeypatch.setattr(eval_mod, "_build_adapter_for_mode", lambda _db, _mode: stub)

        out = run_eval_command(_parsed("eval --baseline", db=db))
        assert "Result:" in out
        assert stub.call_count == len(SAMPLE_QUERIES)


class TestRunnerErrorPaths:
    """The handler renders ``EvalRunnerError`` / generic failures as text."""

    def test_missing_query_set_renders_failed_block(self, db: sqlite3.Connection) -> None:
        """An unregistered query set → ``failed`` block, kind ``missing_query_set``.

        The runner raises ``EvalRunnerError(kind="missing_query_set")``;
        the handler catches it and renders the kind so the operator can
        tell "set not registered" apart from a generic failure.
        """
        out = run_eval_command(
            _parsed("eval --mode fts_only --query-set no-such-set --version 1", db=db)
        )
        assert "status: failed" in out
        assert "kind: missing_query_set" in out
        assert "is not registered" in out

    def test_empty_query_set_renders_failed_block(self, db: sqlite3.Connection) -> None:
        """A registered-but-empty query set → kind ``empty_query_set``.

        ``register_query_set`` rejects empty sets, so the header row is
        inserted directly (matching ``tests/operator/test_eval_runner.py``).
        """
        db.execute(
            "INSERT INTO lcm_eval_query_set (query_set_id, version) VALUES (?, ?)",
            ("empty-set@v1", 1),
        )
        out = run_eval_command(
            _parsed("eval --mode fts_only --query-set empty-set --version 1", db=db)
        )
        assert "status: failed" in out
        assert "kind: empty_query_set" in out

    def test_adapter_exception_renders_failed_block(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-degrading adapter exception → generic ``failed`` block.

        The fts_only adapter does not have the hybrid arm's per-query
        graceful-degrade; if it raises, the runner re-raises (recall
        does not swallow adapter errors) and the handler renders a
        one-line failure rather than crashing.
        """
        register_query_set(db, _identity(), SAMPLE_QUERIES)

        class _ExplodingAdapter:
            async def search(self, _query: Any) -> list[str]:
                raise RuntimeError("simulated retrieval failure")

        monkeypatch.setattr(
            eval_mod, "_build_adapter_for_mode", lambda _db, _mode: _ExplodingAdapter()
        )
        out = run_eval_command(
            _parsed("eval --mode fts_only --query-set test-set --version 1", db=db)
        )
        assert "status: failed" in out
        assert "simulated retrieval failure" in out


# ===========================================================================
# Mode-specific note / warning sections
# ===========================================================================


class TestModeNotes:
    """``semantic_only`` note + ``hybrid`` vec0-absent warning sections."""

    def test_semantic_only_renders_first_cut_note(self, db: sqlite3.Connection) -> None:
        """`--mode semantic_only` surfaces the v4.1-first-cut note.

        The migrated test DB has no vec0, so semantic_only also surfaces
        the vec0 warning — both note sections are expected.
        """
        out = run_eval_command(_parsed("eval --mode semantic_only", db=db))
        assert "Note:" in out
        assert "semantic_only" in out
        assert "hybrid adapter" in out

    def test_hybrid_without_vec0_renders_warning(self, db: sqlite3.Connection) -> None:
        """`--mode hybrid` on a vec0-less DB surfaces the degrade warning."""
        out = run_eval_command(_parsed("eval --mode hybrid", db=db))
        assert "Warning:" in out
        assert "vec0" in out
        assert "degrade to FTS-only" in out

    def test_fts_only_has_no_note_or_warning(self, db: sqlite3.Connection) -> None:
        """`--mode fts_only` renders neither a Note nor a Warning section."""
        out = run_eval_command(_parsed("eval --mode fts_only", db=db))
        assert "Note:" not in out
        assert "Warning:" not in out
