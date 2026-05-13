"""Tests for :func:`parse_lcm_command` — the ``/lcm`` token splitter.

Per the issue 08-01 acceptance criterion (line 64):

> parse_lcm_command(raw_args) honors --reason "quoted value", --from a,b,c,
> bare flags, and unknown flags (raises LcmCommandParseError with the
> offending token).

And line 72:

> New test: tests/commands/test_parse_lcm_command.py — token splitter
> quoting/list/flag invariants per plugin-glue.md §"Test inventory" line 590.

The TS reference is ``lossless-claw/src/plugin/lcm-command.ts:245``
(``splitArgsQuoted``) and ``parseLcmCommand`` at line 422 — the Python
port is a pure-function translation honoring the same flag shapes.
"""

from __future__ import annotations

import pytest

from lossless_hermes.plugin.commands import (
    LcmCommandParseError,
    parse_lcm_command,
)


# ---------------------------------------------------------------------------
# Empty / whitespace inputs
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty_name() -> None:
    """Bare ``/lcm`` (empty raw_args) returns name='' for the dispatcher to alias."""
    parsed = parse_lcm_command("")
    assert parsed.name == ""
    assert parsed.tokens == []
    assert parsed.flags == {}


def test_whitespace_only_returns_empty_name() -> None:
    """Whitespace-only raw_args also returns name='' (no subcommand typed)."""
    parsed = parse_lcm_command("   ")
    assert parsed.name == ""


def test_none_raw_args_returns_empty_name() -> None:
    """Defensive: ``None`` should not crash the parser."""
    parsed = parse_lcm_command(None)
    assert parsed.name == ""


# ---------------------------------------------------------------------------
# Simple subcommand routing
# ---------------------------------------------------------------------------


def test_status_subcommand() -> None:
    """``status`` matches the ``status`` canonical path."""
    parsed = parse_lcm_command("status")
    assert parsed.name == "status"


def test_purge_subcommand_with_no_args() -> None:
    """``purge`` (bare) matches ``purge`` and leaves tokens empty."""
    parsed = parse_lcm_command("purge")
    assert parsed.name == "purge"
    assert parsed.tokens == []


def test_unknown_subcommand_returns_unknown_marker() -> None:
    """Subcommands not in :data:`_SUBCOMMANDS` return ``name='<unknown>'``."""
    parsed = parse_lcm_command("absolutely-not-real")
    assert parsed.name == "<unknown>"


# ---------------------------------------------------------------------------
# Longest-prefix match — nested subcommands
# ---------------------------------------------------------------------------


def test_doctor_apply_matches_two_token_path() -> None:
    """``doctor apply`` matches the 2-token canonical path, not just ``doctor``."""
    parsed = parse_lcm_command("doctor apply")
    assert parsed.name == "doctor apply"
    assert parsed.tokens == []


def test_doctor_clean_apply_matches_three_token_path() -> None:
    """``doctor clean apply`` matches the 3-token canonical path."""
    parsed = parse_lcm_command("doctor clean apply")
    assert parsed.name == "doctor clean apply"


def test_doctor_clean_matches_two_token_path_not_three() -> None:
    """``doctor clean`` (no ``apply``) matches the 2-token cleaners-scan path."""
    parsed = parse_lcm_command("doctor clean")
    assert parsed.name == "doctor clean"


def test_doctor_alone_matches_one_token_path() -> None:
    """``doctor`` (no nested args) matches the bare-doctor path (run_scan)."""
    parsed = parse_lcm_command("doctor")
    assert parsed.name == "doctor"


def test_worker_alone_matches_worker_status_path() -> None:
    """``worker`` (alias) matches the same dispatch as ``worker status``."""
    parsed = parse_lcm_command("worker")
    assert parsed.name == "worker"


def test_worker_tick_embedding_backfill_matches_three_token_path() -> None:
    """``worker tick embedding-backfill`` matches the 3-token tick-backfill path."""
    parsed = parse_lcm_command("worker tick embedding-backfill")
    assert parsed.name == "worker tick embedding-backfill"


# ---------------------------------------------------------------------------
# Flag parsing — --reason quoting
# ---------------------------------------------------------------------------


def test_reason_flag_with_quoted_value() -> None:
    """``--reason "multi-word value"`` is captured as a single string."""
    parsed = parse_lcm_command('purge --reason "multi word reason"')
    assert parsed.name == "purge"
    assert parsed.flags["reason"] == "multi word reason"


def test_reason_flag_with_single_word_value() -> None:
    """``--reason value`` works without quotes when the value is single-word."""
    parsed = parse_lcm_command("purge --reason cleanup")
    assert parsed.flags["reason"] == "cleanup"


def test_reason_flag_missing_value_raises() -> None:
    """``--reason`` at end of tokens raises :class:`LcmCommandParseError`."""
    with pytest.raises(LcmCommandParseError, match="--reason"):
        parse_lcm_command("purge --reason")


# ---------------------------------------------------------------------------
# Flag parsing — --from comma-lists
# ---------------------------------------------------------------------------


def test_from_flag_comma_list() -> None:
    """``--from k1,k2,k3`` is captured as ``list[str]``."""
    parsed = parse_lcm_command("reconcile-session-keys --apply --from k1,k2,k3 --to k4 --reason r")
    # The router consumes "reconcile-session-keys --apply" as the
    # canonical path; remaining tokens carry --from/--to/--reason.
    assert parsed.name == "reconcile-session-keys --apply"
    assert parsed.flags["from"] == ["k1", "k2", "k3"]


def test_from_flag_strips_whitespace_from_entries() -> None:
    """``--from k1, k2 , k3`` strips whitespace per the TS source."""
    parsed = parse_lcm_command(
        "reconcile-session-keys --apply --from 'k1, k2 , k3' --to k4 --reason r"
    )
    assert parsed.flags["from"] == ["k1", "k2", "k3"]


def test_from_flag_filters_empty_entries() -> None:
    """``--from k1,,k2`` filters out empty list entries."""
    parsed = parse_lcm_command("reconcile-session-keys --apply --from k1,,k2 --to k3 --reason r")
    assert parsed.flags["from"] == ["k1", "k2"]


def test_from_flag_missing_value_raises() -> None:
    """``--from`` at end of tokens raises :class:`LcmCommandParseError`."""
    with pytest.raises(LcmCommandParseError, match="--from"):
        parse_lcm_command("reconcile-session-keys --apply --from")


# ---------------------------------------------------------------------------
# Flag parsing — bare flags
# ---------------------------------------------------------------------------


def test_apply_flag_bare() -> None:
    """``--apply`` is captured as ``True``."""
    parsed = parse_lcm_command("purge --apply --reason r")
    assert parsed.flags.get("apply") is True


def test_baseline_flag_bare() -> None:
    """``--baseline`` is captured as ``True``."""
    parsed = parse_lcm_command("eval --baseline")
    assert parsed.flags.get("baseline") is True


def test_allow_main_session_flag_bare() -> None:
    """``--allow-main-session`` becomes ``flags["allow_main_session"] = True``."""
    parsed = parse_lcm_command("purge --apply --allow-main-session --reason r")
    assert parsed.flags.get("allow_main_session") is True


def test_vacuum_flag_bare() -> None:
    """``--vacuum`` is captured as ``True``."""
    parsed = parse_lcm_command("doctor clean apply --vacuum")
    assert parsed.flags.get("vacuum") is True


def test_list_candidates_flag_bare() -> None:
    """``--list-candidates`` is captured as ``True``."""
    parsed = parse_lcm_command("reconcile-session-keys --list-candidates")
    assert parsed.flags.get("list_candidates") is True
    assert parsed.name == "reconcile-session-keys --list-candidates"


# ---------------------------------------------------------------------------
# Error paths — unbalanced quotes
# ---------------------------------------------------------------------------


def test_unbalanced_quotes_raise_parse_error() -> None:
    """Unbalanced double quotes raise :class:`LcmCommandParseError`."""
    with pytest.raises(LcmCommandParseError, match="argument parse error"):
        parse_lcm_command('purge --reason "unclosed')


def test_unbalanced_single_quotes_raise_parse_error() -> None:
    """Unbalanced single quotes also raise."""
    with pytest.raises(LcmCommandParseError, match="argument parse error"):
        parse_lcm_command("purge --reason 'unclosed")


# ---------------------------------------------------------------------------
# Raw-args preservation
# ---------------------------------------------------------------------------


def test_raw_args_preserved_for_handler_re_tokenization() -> None:
    """The original raw_args string is preserved on the parsed object.

    Handlers needing per-subcommand flag parsing (purge, reconcile, eval)
    can re-tokenize from raw_args themselves — useful for cases where the
    router's best-effort flag pre-parse missed something.
    """
    raw = 'purge --reason "complex value" --session-key sk1'
    parsed = parse_lcm_command(raw)
    assert parsed.raw_args == raw.strip()


def test_remaining_tokens_carry_through_to_handler() -> None:
    """Tokens after the canonical path are preserved for handler parsing.

    For ``/lcm purge --reason "x" --session-key sk1`` the canonical path
    is ``purge`` (1 token); the remaining 4 tokens stay in
    ``parsed.tokens`` for the purge handler to parse.
    """
    parsed = parse_lcm_command('purge --reason "x" --session-key sk1')
    assert parsed.name == "purge"
    # The 4 remaining tokens are kept (--reason was pre-parsed into
    # flags, but the original tokens are still here).
    assert "--reason" in parsed.tokens
    assert "x" in parsed.tokens
    assert "--session-key" in parsed.tokens
    assert "sk1" in parsed.tokens


# ---------------------------------------------------------------------------
# Case-insensitivity on subcommand matching (TS parity)
# ---------------------------------------------------------------------------


def test_subcommand_case_insensitive() -> None:
    """``STATUS`` and ``Status`` both match the ``status`` canonical path.

    Mirrors the TS source line 429: ``switch (head.toLowerCase())``.
    """
    assert parse_lcm_command("STATUS").name == "status"
    assert parse_lcm_command("Status").name == "status"
    assert parse_lcm_command("DOCTOR APPLY").name == "doctor apply"
