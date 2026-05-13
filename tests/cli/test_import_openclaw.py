"""Tests for ``lossless-hermes import-openclaw`` CLI (issue 08-15).

Covers the acceptance criteria from
``epics/08-cli-ops/08-15-import-openclaw-cli.md`` lines 83-104:

* Argparse surface matches ADR-025 (``--from``, ``--to``, ``--force``,
  ``--validate-rows``, ``--dry-run``).
* Defaults: ``--from=~/.openclaw``, ``--to=~/.hermes/lossless-hermes``.
* Destination-DB-exists refusal without ``--force`` (with summary text).
* ``--dry-run`` touches nothing.
* ``shutil.copy2`` semantics (full copy, not symlink, not move).
* ``lcm-files/`` → ``large-files/`` rename (ADR-001/002).
* ``voyage-api-key`` chmod 0o600; parent dir 0o700.
* ``run_lcm_migrations()`` runs on destination.
* identity-hash sample validation with default 100 rows.
* ``state_meta.lcm_db_imported_at`` written on success.
* Idempotency: re-run without ``--force`` exits 1 and does not duplicate
  state_meta rows.
* Schema-newer-than-supported produces a clean error.
* ``/lcm import-openclaw`` slash alias routes to :func:`run_slash`.
* Standalone CLI invocation bypasses the gateway gate (it never imports
  ``slash_access.SlashAccessPolicy``).
"""

from __future__ import annotations

import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Iterator

import pytest

from lossless_hermes.cli import main as cli_main
from lossless_hermes.cli.import_openclaw import (
    ImportResult,
    build_parser,
    import_openclaw,
    main,
    run_slash,
)
from lossless_hermes.db.connection import close_lcm_connection, open_lcm_db
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.message_identity import build_message_identity_hash

# ---------------------------------------------------------------------------
# Skip marker: actions/setup-python macOS builds lack enable_load_extension
# ---------------------------------------------------------------------------
#
# Per ADR-004 §Open questions item 1 and ADR-028 §Decision point 8, the
# actions/setup-python macOS pre-built CPython ships without
# ``--enable-loadable-sqlite-extensions``. The import command opens a
# real ``open_lcm_db`` connection (so it can run the migration ladder
# against the destination), which fires the Apple-Python guard on those
# runners. The pure-argparse tests (parser surface, defaults) and the
# AST-scan import-graph tests still run — they don't touch the DB.
# Mirrors the ``_skip_no_extension_loading`` pattern in
# ``tests/test_lifecycle.py`` and ``tests/test_engine_ingest.py``.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load, so "
        "open_lcm_db() raises the Apple-Python guard. See ADR-004 §Open "
        "questions item 1 + ADR-028 §Decision point 8."
    ),
)


# ---------------------------------------------------------------------------
# Fixture: a "mini OpenClaw" tree on disk
# ---------------------------------------------------------------------------


def _build_openclaw_db(path: Path, *, message_count: int = 100) -> None:
    """Build a small but realistic OpenClaw-shape lcm.db at ``path``.

    Runs the full migration ladder (so the schema matches what
    OpenClaw would have produced) then seeds N message rows with valid
    ``identity_hash`` values. The lossless-hermes migration is byte-
    compatible with the OpenClaw shape per ADR-026 — running it again
    during import should be a no-op.
    """
    conn = open_lcm_db(path)
    try:
        run_lcm_migrations(conn)
        conn.execute(
            "INSERT INTO conversations (session_id, session_key, active, title) "
            "VALUES (?, ?, 1, ?)",
            ("session-mini", "key-mini", "Mini OpenClaw fixture"),
        )
        conv_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Insert N messages with valid identity_hash.
        for i in range(message_count):
            role = "user" if i % 2 == 0 else "assistant"
            content = f"message {i} content - aabbcc {i * 7}"
            identity = build_message_identity_hash(role, content)
            conn.execute(
                "INSERT INTO messages (conversation_id, seq, role, content, "
                "token_count, identity_hash) VALUES (?, ?, ?, ?, ?, ?)",
                (conv_id, i, role, content, len(content) // 4, identity),
            )
        conn.commit()
    finally:
        close_lcm_connection(path)


@pytest.fixture
def openclaw_root(tmp_path: Path) -> Iterator[Path]:
    """A ``tmp_path/openclaw`` tree shaped like ``~/.openclaw/``.

    Contains:
    * ``lcm.db`` with 100 valid messages (matching 08-15 spec's
      ``openclaw-mini`` fixture).
    * ``lcm-files/blob1.bin`` placeholder (verifies the rename).
    * ``credentials/voyage-api-key`` placeholder (verifies chmod).

    The fixture closes any open connections after the test body via
    :func:`close_lcm_connection` so the lcm.db file can be re-opened
    by the import body.

    Skips on macOS GH-Actions runners that lack ``enable_load_extension``
    (see module-level ``_skip_no_extension_loading``).
    """
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        pytest.skip(
            "actions/setup-python on macOS ships a CPython build without "
            "--enable-loadable-sqlite-extensions; cannot build OpenClaw "
            "fixture DB. See ADR-004 §Open questions item 1."
        )
    root = tmp_path / "openclaw"
    root.mkdir()
    db_path = root / "lcm.db"
    _build_openclaw_db(db_path, message_count=100)

    (root / "lcm-files").mkdir()
    (root / "lcm-files" / "blob1.bin").write_bytes(b"large-file-payload-1")
    (root / "lcm-files" / "blob2.bin").write_bytes(b"large-file-payload-2")

    (root / "credentials").mkdir()
    (root / "credentials" / "voyage-api-key").write_text("pa-test-voyage-key", encoding="utf-8")

    # Belt-and-suspenders: close every open connection for this path
    # before the test body so the import command can re-open it.
    close_lcm_connection(db_path)

    try:
        yield root
    finally:
        close_lcm_connection(db_path)


@pytest.fixture
def dest_root(tmp_path: Path) -> Path:
    """Empty destination directory for an import target."""
    return tmp_path / "hermes" / "lossless-hermes"


# ---------------------------------------------------------------------------
# Acceptance: argparse surface (ADR-025 + spec line 83)
# ---------------------------------------------------------------------------


class TestArgparseSurface:
    """Argparse surface matches ADR-025."""

    def test_parser_has_all_documented_flags(self) -> None:
        parser = build_parser()
        # Use the parser's internal action map to enumerate documented
        # flags without depending on argparse internals.
        flags: list[str] = []
        for action in parser._actions:
            flags.extend(action.option_strings)
        for expected in ("--from", "--to", "--force", "--validate-rows", "--dry-run"):
            assert expected in flags, f"missing flag {expected!r}"

    def test_defaults_match_adr025(self) -> None:
        parser = build_parser()
        ns = parser.parse_args([])
        assert ns.source == "~/.openclaw"
        assert ns.destination == "~/.hermes/lossless-hermes"
        assert ns.force is False
        assert ns.validate_rows == 100
        assert ns.dry_run is False

    def test_validate_rows_is_int(self) -> None:
        parser = build_parser()
        ns = parser.parse_args(["--validate-rows", "500"])
        assert ns.validate_rows == 500


# ---------------------------------------------------------------------------
# Acceptance: full round trip (spec line 97)
# ---------------------------------------------------------------------------


def test_full_round_trip(openclaw_root: Path, dest_root: Path) -> None:
    """End-to-end import: schema, identity-hash sample, state_meta written.

    Per the issue spec's primary acceptance test:

    > copy tests/fixtures/openclaw-mini/lcm.db (100-conv fixture), assert
    > schema migrated, 100/100 identity-hash sample matched,
    > state_meta.lcm_db_imported_at written.

    Our fixture has 100 messages (one conversation) — the validation is
    the same: 100 sampled rows match.
    """
    result = import_openclaw(
        source=openclaw_root,
        destination=dest_root,
        validate_rows=100,
    )
    assert result.ok, f"import failed: {result.error}\n{result.report}"
    assert result.dry_run is False
    assert result.validated == 100
    assert result.matched == 100
    assert result.mismatched == 0

    # Destination DB exists and was migrated.
    dest_db = dest_root / "lcm.db"
    assert dest_db.exists()
    conn = sqlite3.connect(dest_db)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        # core tables present
        assert "conversations" in tables
        assert "messages" in tables
        # state_meta table created by the import body
        assert "state_meta" in tables
        # state_meta row written
        row = conn.execute(
            "SELECT key, value FROM state_meta WHERE key='lcm_db_imported_at'"
        ).fetchone()
        assert row is not None
        assert row[0] == "lcm_db_imported_at"
        assert row[1] is not None and row[1] != ""
    finally:
        conn.close()
        close_lcm_connection(dest_db)


# ---------------------------------------------------------------------------
# Acceptance: refuse without --force (spec line 98)
# ---------------------------------------------------------------------------


def test_refuse_without_force(openclaw_root: Path, dest_root: Path) -> None:
    """Destination DB already exists → exit 1 with refusal message."""
    # First import to populate the destination.
    first = import_openclaw(source=openclaw_root, destination=dest_root)
    assert first.ok, first.error

    # Re-run without --force should refuse.
    second = import_openclaw(source=openclaw_root, destination=dest_root)
    assert second.ok is False
    assert "refus" in second.error.lower() or "exists" in second.error.lower()
    assert "--force" in second.error.lower()
    # The refusal text must include the existing-data summary.
    assert "conversation" in second.error.lower()


def test_refuse_via_main_returns_exit_1(
    openclaw_root: Path, dest_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI ``main`` returns exit code 1 on refusal."""
    rc1 = main(["--from", str(openclaw_root), "--to", str(dest_root)])
    assert rc1 == 0

    rc2 = main(["--from", str(openclaw_root), "--to", str(dest_root)])
    assert rc2 == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err.lower()


# ---------------------------------------------------------------------------
# Acceptance: --dry-run touches nothing (spec line 99)
# ---------------------------------------------------------------------------


def test_dry_run_touches_nothing(openclaw_root: Path, dest_root: Path) -> None:
    """``--dry-run`` invocation creates no files in destination."""
    assert not dest_root.exists()
    result = import_openclaw(
        source=openclaw_root,
        destination=dest_root,
        dry_run=True,
    )
    assert result.ok
    assert result.dry_run is True
    assert "dry-run" in result.report.lower()
    # No files should have been created.
    assert not dest_root.exists() or not any(dest_root.iterdir())


# ---------------------------------------------------------------------------
# Acceptance: voyage-api-key chmod (spec line 100)
# ---------------------------------------------------------------------------


def test_voyage_api_key_chmod(openclaw_root: Path, dest_root: Path) -> None:
    """Credentials copied with mode 0o600; parent dir 0o700."""
    if sys.platform == "win32":
        pytest.skip("POSIX file modes not enforced on Windows")
    result = import_openclaw(source=openclaw_root, destination=dest_root)
    assert result.ok, result.error

    dst_cred = dest_root / "credentials" / "voyage-api-key"
    assert dst_cred.exists()
    mode = stat.S_IMODE(os.stat(dst_cred).st_mode)
    assert mode == 0o600, f"voyage-api-key mode {oct(mode)} != 0o600"

    dst_dir = dest_root / "credentials"
    dir_mode = stat.S_IMODE(os.stat(dst_dir).st_mode)
    assert dir_mode == 0o700, f"credentials/ dir mode {oct(dir_mode)} != 0o700"


# ---------------------------------------------------------------------------
# Acceptance: state_meta idempotent (spec line 101)
# ---------------------------------------------------------------------------


def test_idempotent_state_meta(openclaw_root: Path, dest_root: Path) -> None:
    """Re-running without --force doesn't duplicate state_meta rows.

    Per ADR-025 §Open questions #3 + the spec's 10% risk: the
    ``--force`` path should overwrite the row, not duplicate it.
    Without ``--force`` the import is refused early so no second row
    gets written. We exercise BOTH branches to lock the contract.
    """
    # First import populates state_meta.
    first = import_openclaw(source=openclaw_root, destination=dest_root)
    assert first.ok, first.error
    dest_db = dest_root / "lcm.db"

    def _count_state_rows() -> int:
        conn = sqlite3.connect(dest_db)
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM state_meta WHERE key='lcm_db_imported_at'"
                ).fetchone()[0]
            )
        finally:
            conn.close()
            close_lcm_connection(dest_db)

    assert _count_state_rows() == 1

    # No-force second run: refused; state_meta unchanged.
    second = import_openclaw(source=openclaw_root, destination=dest_root)
    assert not second.ok
    assert _count_state_rows() == 1

    # Force third run: overwrites, still 1 row (ON CONFLICT UPSERT).
    third = import_openclaw(source=openclaw_root, destination=dest_root, force=True)
    assert third.ok, third.error
    assert _count_state_rows() == 1


# ---------------------------------------------------------------------------
# Layout-rename: lcm-files/ → large-files/
# ---------------------------------------------------------------------------


def test_large_files_rename(openclaw_root: Path, dest_root: Path) -> None:
    """``lcm-files/`` source becomes ``large-files/`` at destination."""
    result = import_openclaw(source=openclaw_root, destination=dest_root)
    assert result.ok, result.error

    assert (dest_root / "large-files").exists()
    assert not (dest_root / "lcm-files").exists()
    assert (dest_root / "large-files" / "blob1.bin").read_bytes() == b"large-file-payload-1"
    assert (dest_root / "large-files" / "blob2.bin").read_bytes() == b"large-file-payload-2"


# ---------------------------------------------------------------------------
# Source-missing branch
# ---------------------------------------------------------------------------


def test_missing_source_dir(tmp_path: Path) -> None:
    """Nonexistent source path → ok=False with diagnostic error."""
    result = import_openclaw(
        source=tmp_path / "does-not-exist",
        destination=tmp_path / "dest",
    )
    assert not result.ok
    assert "does not exist" in result.error.lower()


def test_source_missing_lcm_db(tmp_path: Path) -> None:
    """Source directory exists but has no lcm.db → ok=False."""
    src = tmp_path / "empty-openclaw"
    src.mkdir()
    result = import_openclaw(source=src, destination=tmp_path / "dest")
    assert not result.ok
    assert "does not contain lcm.db" in result.error.lower()


# ---------------------------------------------------------------------------
# Identity-hash mismatch is reported but non-fatal (ADR-025 line 91)
# ---------------------------------------------------------------------------


def test_identity_hash_mismatch_is_nonfatal(openclaw_root: Path, dest_root: Path) -> None:
    """Some rows with intentionally-wrong identity_hash are non-fatal."""
    # Mutate one row's identity_hash to a known-bad value so the sample
    # validation reports a mismatch.
    src_db = openclaw_root / "lcm.db"
    conn = sqlite3.connect(src_db)
    try:
        conn.execute(
            "UPDATE messages SET identity_hash='deadbeef' "
            "WHERE message_id IN (SELECT message_id FROM messages LIMIT 5)"
        )
        conn.commit()
    finally:
        conn.close()
        close_lcm_connection(src_db)

    result = import_openclaw(
        source=openclaw_root,
        destination=dest_root,
        validate_rows=100,
    )
    assert result.ok, result.error
    assert result.validated == 100
    # At least the 5 we mutated should be mismatched; possibly more if
    # the random sample picks them up. Asserting >= 0 is too loose; we
    # require the mismatched count to be >= 1 since the sample is the
    # full 100 rows (and we mutated 5 of them).
    assert result.mismatched >= 1
    assert result.matched + result.mismatched == result.validated


# ---------------------------------------------------------------------------
# Slash-command bridge
# ---------------------------------------------------------------------------


class TestSlashBridge:
    """``/lcm import-openclaw`` slash entry routes to :func:`run_slash`."""

    def test_run_slash_invokes_import(self, openclaw_root: Path, dest_root: Path) -> None:
        from lossless_hermes.plugin.commands import parse_lcm_command

        raw = f"import-openclaw --from {openclaw_root} --to {dest_root} --validate-rows 10"
        parsed = parse_lcm_command(raw)
        assert parsed.name == "import-openclaw"
        out = run_slash(parsed)
        assert "Import complete" in out
        assert (dest_root / "lcm.db").exists()

    def test_run_slash_handles_dry_run(self, openclaw_root: Path, dest_root: Path) -> None:
        from lossless_hermes.plugin.commands import parse_lcm_command

        raw = f"import-openclaw --from {openclaw_root} --to {dest_root} --dry-run"
        parsed = parse_lcm_command(raw)
        out = run_slash(parsed)
        assert "dry-run" in out.lower()
        assert not dest_root.exists() or not any(dest_root.iterdir())

    def test_run_slash_does_not_inspect_owner_context(self) -> None:
        """Per ADR-013 the handler must not look up owner status.

        The module imports nothing from ``slash_access``: verified by
        scanning the module's actual ``import`` statements (docstring
        mentions of the policy by name are fine — design docs explain
        the upstream gate).
        """
        import ast

        import lossless_hermes.cli.import_openclaw as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported_names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.append(node.module or "")
                imported_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported_names.extend(alias.name for alias in node.names)
        joined = " ".join(imported_names)
        assert "slash_access" not in joined
        assert "SlashAccessPolicy" not in joined


# ---------------------------------------------------------------------------
# Standalone CLI bypasses gateway (ADR-013 + spec line 96)
# ---------------------------------------------------------------------------


def test_cli_main_does_not_import_slash_access(openclaw_root: Path, dest_root: Path) -> None:
    """``lossless-hermes import-openclaw`` from a shell never hits the gate.

    The ``main`` function is invoked here in-process; we verify by
    inspecting the module's import graph (docstring mentions of the
    policy in design notes are fine — actual ``import`` statements are
    what would route control through the gate).
    """
    import ast

    rc = cli_main(["import-openclaw", "--from", str(openclaw_root), "--to", str(dest_root)])
    assert rc == 0
    import lossless_hermes.cli as cli_pkg
    import lossless_hermes.cli.import_openclaw as imp_mod

    for mod in (cli_pkg, imp_mod):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        joined = " ".join(imported)
        assert "slash_access" not in joined, f"{mod.__name__} imports slash_access"


def test_cli_help_returns_zero() -> None:
    """``lossless-hermes --help`` exits 0 (no subcommand required)."""
    rc = cli_main(["--help"])
    assert rc == 0


def test_cli_with_no_args_prints_help() -> None:
    """``lossless-hermes`` alone returns non-zero with help text."""
    rc = cli_main([])
    assert rc == 2


# ---------------------------------------------------------------------------
# Schema-newer error path (ADR-025 §"Open questions" #4)
# ---------------------------------------------------------------------------


def test_schema_newer_error_message(
    openclaw_root: Path, dest_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A future-schema DB surfaces a clean error, not a raw OperationalError."""

    # Simulate a "schema newer than supported" by patching
    # run_lcm_migrations to raise a DatabaseError whose message looks
    # like an UNKNOWN-column ALTER.
    def _fake_run(conn: sqlite3.Connection, **kwargs: object) -> None:
        raise sqlite3.OperationalError("no such column: messages.future_v4_2_column")

    monkeypatch.setattr(
        "lossless_hermes.cli.import_openclaw.run_lcm_migrations",
        _fake_run,
    )
    result = import_openclaw(source=openclaw_root, destination=dest_root)
    assert not result.ok
    assert "newer than this port supports" in result.error


# ---------------------------------------------------------------------------
# build_message_identity_hash invariant
# ---------------------------------------------------------------------------


def test_imported_db_messages_match_recomputed_hashes(openclaw_root: Path, dest_root: Path) -> None:
    """After import, every message row's stored hash recomputes byte-identically.

    Locks the Spike-003 invariant on the imported corpus. Not strictly
    in the spec's enumerated tests, but it's the load-bearing
    correctness check that motivates the whole migration story.
    """
    result = import_openclaw(source=openclaw_root, destination=dest_root)
    assert result.ok

    dest_db = dest_root / "lcm.db"
    conn = sqlite3.connect(dest_db)
    try:
        rows = list(
            conn.execute(
                "SELECT role, content, identity_hash FROM messages WHERE identity_hash IS NOT NULL"
            )
        )
    finally:
        conn.close()
        close_lcm_connection(dest_db)
    assert len(rows) == 100
    for role, content, stored in rows:
        assert build_message_identity_hash(role, content) == stored


# ---------------------------------------------------------------------------
# Optional sources: lcm-files / credentials absence
# ---------------------------------------------------------------------------


@_skip_no_extension_loading
def test_missing_lcm_files_skipped_cleanly(tmp_path: Path) -> None:
    """Source has no lcm-files/ → skipped, not an error."""
    src = tmp_path / "minimal"
    src.mkdir()
    _build_openclaw_db(src / "lcm.db", message_count=5)
    close_lcm_connection(src / "lcm.db")

    dest = tmp_path / "dest"
    result = import_openclaw(source=src, destination=dest)
    assert result.ok, result.error
    assert "no lcm-files/" in result.report or "skipping" in result.report.lower()


@_skip_no_extension_loading
def test_missing_credentials_skipped_cleanly(tmp_path: Path) -> None:
    """Source has no credentials/ → skipped, not an error."""
    src = tmp_path / "minimal"
    src.mkdir()
    _build_openclaw_db(src / "lcm.db", message_count=5)
    close_lcm_connection(src / "lcm.db")

    dest = tmp_path / "dest"
    result = import_openclaw(source=src, destination=dest)
    assert result.ok, result.error
    assert "no credentials" in result.report or "skipping" in result.report.lower()


# ---------------------------------------------------------------------------
# Result type sanity
# ---------------------------------------------------------------------------


def test_import_result_dataclass_fields() -> None:
    """Sanity-check :class:`ImportResult` exposes the documented fields."""
    r = ImportResult(ok=True, dry_run=False, report="hi", validated=5, matched=5)
    assert r.ok and not r.dry_run
    assert r.report == "hi"
    assert r.mismatched == 0
    assert r.validated == 5
