"""Tests for ``/lcm backup`` — issue 08-09.

Covers :mod:`lossless_hermes.commands.backup`'s rendering of the four
branches in the TS ``buildBackupText`` (``lcm-command.ts:1356-1411``):

* ``engine._db is None`` — engine not yet initialized.
* In-memory / empty database path — "unavailable" with the
  "file-backed required" reason (mirrors TS
  ``getLcmBackupUnavailableReason``).
* Backup primitive raises :class:`LcmDatabaseBackupError` — "failed"
  with the underlying reason.
* Happy path — "created" with the db path and backup path.

See:

* ``epics/08-cli-ops/08-09-backup.md`` — issue spec.
* ``lossless-claw/src/plugin/lcm-command.ts:1356-1411`` — TS source.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lossless_hermes.commands.backup import run


def _make_engine(
    db: sqlite3.Connection | None = None,
    database_path: str = "",
) -> Any:
    """Minimal engine stub. ``commands.backup`` reads only
    ``engine._db`` + ``engine.config.database_path``.
    """
    return SimpleNamespace(
        _db=db,
        config=SimpleNamespace(database_path=database_path),
    )


def _make_parsed(engine: Any) -> Any:
    """Build a ``ParsedLcmCommand``-shaped namespace.

    The dispatcher attaches ``engine`` to the parsed object before
    invoking the handler; we mirror that here.
    """
    return SimpleNamespace(engine=engine, name="backup", raw_args="")


# ---------------------------------------------------------------------------
# Engine-not-initialized branch
# ---------------------------------------------------------------------------


def test_engine_none_returns_dispatcher_misconfigured() -> None:
    """Missing ``parsed.engine`` is a programmer error — render a clear msg."""
    parsed = SimpleNamespace(name="backup", raw_args="")
    out = run(parsed)
    assert "dispatcher misconfigured" in out


def test_no_db_open_returns_unavailable() -> None:
    """``engine._db is None`` (pre-on_session_start) renders "unavailable"."""
    engine = _make_engine(db=None, database_path="/some/path.db")
    out = run(_make_parsed(engine))
    assert "status: unavailable" in out
    assert "Engine not yet initialized" in out


# ---------------------------------------------------------------------------
# Unavailable-path branches (in-memory / empty)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        ":memory:",
        "file::memory:?cache=shared",
    ],
)
def test_in_memory_or_empty_path_returns_unavailable(path: str) -> None:
    """In-memory/empty paths render "unavailable" with the file-backed reason."""
    conn = sqlite3.connect(":memory:")
    try:
        engine = _make_engine(db=conn, database_path=path)
        out = run(_make_parsed(engine))
        assert "status: unavailable" in out
        assert "file-backed SQLite database" in out
    finally:
        conn.close()


def test_invalid_path_type_returns_unavailable() -> None:
    """Defensive path: non-string database_path renders "Invalid database path."."""
    conn = sqlite3.connect(":memory:")
    try:
        engine = _make_engine(db=conn, database_path=None)  # type: ignore[arg-type]
        out = run(_make_parsed(engine))
        assert "status: unavailable" in out
        assert "Invalid database path" in out
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Happy path — full backup roundtrip
# ---------------------------------------------------------------------------


def test_happy_path_renders_created(tmp_path: Path) -> None:
    """A file-backed DB renders "status: created / db path: ... / backup path: ...".

    The backup file is then deleted by ``tmp_path`` cleanup.
    """
    db_path = tmp_path / "lcm.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO messages DEFAULT VALUES")
        conn.commit()

        engine = _make_engine(db=conn, database_path=str(db_path))
        out = run(_make_parsed(engine))

        assert "status: created" in out
        assert f"db path: {db_path}" in out
        # backup path line should exist and contain the .bak suffix.
        backup_line = next(
            line for line in out.splitlines() if line.strip().startswith("backup path:")
        )
        assert backup_line.endswith(".bak")
        # The backup file should actually exist on disk.
        backup_path_str = backup_line.split("backup path:", 1)[1].strip()
        assert Path(backup_path_str).is_file()
    finally:
        conn.close()


def test_failure_renders_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``LcmDatabaseBackupError`` from the primitive renders "status: failed".

    Simulates a primitive failure by pre-creating a directory at the
    deterministic destination path (forcing ``VACUUM INTO`` to fail).
    """
    from lossless_hermes.plugin import db_backup as db_backup_mod

    db_path = tmp_path / "lcm.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()

        # Force deterministic suffix and pre-create the target as a directory.
        monkeypatch.setattr(db_backup_mod.secrets, "token_hex", lambda n: "deadbe")
        target = db_backup_mod.build_lcm_database_backup_path(
            str(db_path),
            label="backup",
        )
        target.mkdir(parents=True, exist_ok=False)

        engine = _make_engine(db=conn, database_path=str(db_path))
        out = run(_make_parsed(engine))

        assert "status: failed" in out
        assert "VACUUM INTO" in out  # the underlying reason
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Output shape — header + section structure
# ---------------------------------------------------------------------------


def test_output_includes_header_and_section_title(tmp_path: Path) -> None:
    """The rendered output always carries the two-line header and section."""
    db_path = tmp_path / "lcm.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        engine = _make_engine(db=conn, database_path=str(db_path))
        out = run(_make_parsed(engine))
        # Header pattern: starts with "**Lossless Hermes v..."
        lines = out.splitlines()
        assert lines[0].startswith("**Lossless Hermes v")
        # Help line
        assert any("/lcm help" in line for line in lines[:3])
        # Section title
        assert any("Lossless Claw Backup" in line for line in lines)
        # Backup section header
        assert any(line.startswith("**Backup**") for line in lines)
    finally:
        conn.close()
