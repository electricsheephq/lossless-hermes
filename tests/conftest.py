"""Cross-cutting pytest fixtures for the lossless-hermes test suite.

The **shapes** of these fixtures are locked down in this file per
issue 00-04. Bodies for fixtures whose dependencies haven't landed yet
(`db_with_vec0`, `fake_voyage`, `fake_llm`, `test_corpus`) raise
`NotImplementedError` with a pointer to the epic that will wire them.

Fixture inventory follows ADR-028 §"Consequences" and
`docs/porting-guides/tests-and-config.md` §"Common fixtures" lines 152-222:

    tmp_home        — sandboxed HERMES_HOME directory + env var
    db_in_memory    — bare in-memory SQLite connection (no migrations yet)
    db_with_vec0    — in-memory SQLite + sqlite-vec extension (Epic 05)
    fake_voyage     — respx.MockRouter for Voyage HTTP mocking (Epic 05)
    fake_llm        — recording mock LlmCall (Epic 04, ports v41-mock-llm.ts)
    test_corpus     — seeded conversation corpus (Epic 03, ports v41-test-corpus.ts)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import pytest

if TYPE_CHECKING:
    import respx


# ---------------------------------------------------------------------------
# Filesystem sandboxing
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandboxed ``HERMES_HOME`` directory for the duration of the test.

    Replicates the vitest.config.ts trick of rewriting ``$HOME`` to a clean
    tmpdir per run (see `docs/porting-guides/tests-and-config.md` §"Common
    fixtures" lines 152-222 and ADR-028 §"Consequences"). The fixture:

    * Points ``HOME`` at ``tmp_path`` (defensive — some library code reads it).
    * Points ``HERMES_HOME`` at ``tmp_path/.hermes`` (the canonical Hermes
      state-dir env var per `docs/porting-guides/tests-and-config.md`
      §"Hermes-side env-var translation").
    * Pre-creates ``.hermes/`` so test code can drop files there immediately.

    Returns the ``tmp_path`` root so tests can also reach the parent dir.
    """

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return tmp_path


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


@pytest.fixture
def db_in_memory() -> Iterator[sqlite3.Connection]:
    """Bare ``:memory:`` SQLite connection.

    No migrations are applied — the migration ladder lands in Epic 01
    (`run_migrations(conn, ...)` will be invoked from this fixture once
    that subsystem ports). Until then this is just an open connection so
    tests can exercise the seam.

    Mirrors ``new DatabaseSync(':memory:')`` in the TS suite.
    """

    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def db_with_vec0(db_in_memory: sqlite3.Connection) -> sqlite3.Connection:
    """In-memory SQLite with ``sqlite-vec`` extension loaded.

    Auto-skips if vec0 is not loadable on this Python build per ADR-028
    §"Decision point 8" — Homebrew Python on macOS often ships without
    ``enable_load_extension`` enabled.

    Full implementation (extension probe + ``run_migrations`` with
    ``fts5_available=True``) lands in Epic 05 (embeddings/Voyage stack).
    """

    raise NotImplementedError(
        "db_with_vec0: Epic 05 (embeddings) will wire sqlite-vec loading + "
        "skip-on-fail per ADR-028 §Decision 8."
    )


# ---------------------------------------------------------------------------
# HTTP / LLM mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_voyage() -> "respx.MockRouter":
    """``respx.MockRouter`` placeholder for the Voyage HTTP client.

    Will be replaced in Epic 05 with a router that intercepts the Voyage
    embeddings endpoint and serves canned ``EmbedResponse`` payloads
    (per `docs/porting-guides/tests-and-config.md` §"Common fixtures"
    lines 183-196).
    """

    raise NotImplementedError(
        "fake_voyage: Epic 05 (embeddings) will return a respx.MockRouter "
        "scoped to https://api.voyageai.com with canned vectors."
    )


@pytest.fixture
def fake_llm() -> Any:
    """Recording mock ``LlmCall``.

    Ports ``test/fixtures/v41-mock-llm.ts`` (deterministic adversarial-shape
    mock — repertoire: good, fabricated_citations, malformed_json,
    hallucinated_content, empty, throw, rate_limit, verify_OK/HALLUCINATION/
    UNSUPPORTED). Lands in Epic 04 (compaction/summarize stack).
    """

    raise NotImplementedError(
        "fake_llm: Epic 04 (compaction) will port v41-mock-llm.ts -> "
        "tests/fixtures/mock_llm.py and wire MockLlmCall here."
    )


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------


@pytest.fixture
def test_corpus(db_in_memory: sqlite3.Connection) -> dict[str, Any]:
    """Synthetic conversation corpus.

    Ports ``test/fixtures/v41-test-corpus.ts``. Returns a metadata dict
    (BASE_DATE, conversation ids, summary ids) — the underlying rows are
    seeded into the ``db_in_memory`` connection that was just yielded.

    Lands in Epic 03 (storage layer) once ``buildTestCorpus`` ports.
    """

    raise NotImplementedError(
        "test_corpus: Epic 03 (storage) will port v41-test-corpus.ts -> "
        "tests/fixtures/test_corpus.py and seed db_in_memory here."
    )
