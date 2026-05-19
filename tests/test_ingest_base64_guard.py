"""Tests for the ingest-time base64/media externalization guard (issue #131).

Covers the storage-boundary guard added in issue #131:

* :mod:`lossless_hermes.engine.ingest_guard` — the pure detection
  heuristics (:func:`contains_data_uri_base64`,
  :func:`contains_long_base64_run`, :func:`looks_like_long_base64`) and
  the :class:`LargeFileManager`-routed
  :func:`protect_message_for_ingest`.
* :meth:`_IngestMixin._apply_ingest_payload_guard` /
  :meth:`_IngestMixin._ingest_single` wiring — the guard runs before the
  DB transaction so a ``data:image/...;base64,<huge>`` payload never
  lands raw in ``messages.content`` or the ``messages_fts`` shadow.

The guard is a clean reimplementation of
``hermes-lcm/ingest_protection.py:419-496`` (hermes-lcm has no LICENSE),
routing externalized payloads through the existing
:class:`~lossless_hermes.large_files.LargeFileManager` rather than a
parallel store — see ``epics`` issue #131.

References:

* ``hermes-lcm/ingest_protection.py:419-496`` — the reference shape.
* ``lossless-claw/src/engine.ts:5950-6022`` — the TS interception passes.
* ``src/lossless_hermes/engine/ingest_guard.py`` — the guard.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.engine import LCMEngine
from lossless_hermes.engine.ingest_guard import (
    contains_data_uri_base64,
    contains_long_base64_run,
    looks_like_long_base64,
    protect_message_for_ingest,
)
from lossless_hermes.large_files import LargeFileManager

# ---------------------------------------------------------------------------
# Skip marker — mirrors tests/test_engine_ingest.py
# ---------------------------------------------------------------------------
#
# The end-to-end ingest tests run on top of ``on_session_start``'s opened
# DB, which loads sqlite-vec. Apple's system CPython lacks
# ``--enable-loadable-sqlite-extensions``, so those tests skip there. The
# pure-detection + manager-routed unit tests do NOT need the extension and
# run everywhere.
_skip_no_extension_loading = pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "enable_load_extension"),
    reason=(
        "actions/setup-python on macOS ships a CPython build without "
        "--enable-loadable-sqlite-extensions; sqlite-vec cannot load. "
        "End-to-end ingest tests require the full lifecycle DB."
    ),
)


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------
#
# A clean base64 run with a well-mixed alphabet and a length that is a
# multiple of 4 — clears every conservative gate in
# :func:`looks_like_long_base64`.
_B64_UNIT = "ABCDefgh1234+/wXyZ7890"  # 22 chars, 22 distinct-ish chars


def _long_base64(n_chars: int = 8192) -> str:
    """Return a clean base64-alphabet run of ~``n_chars`` characters.

    The run is well-mixed (>= 8 distinct characters), 100% base64
    alphabet, and length-padded to a multiple of 4 so it clears the
    conservative :func:`looks_like_long_base64` gate.
    """
    repeats = (n_chars // len(_B64_UNIT)) + 1
    run = (_B64_UNIT * repeats)[:n_chars]
    # Pad to a multiple of 4 (no valid base64 has length % 4 == 1).
    while len(run) % 4 != 0:
        run += "A"
    return run


def _data_uri(mime: str = "image/png", payload_chars: int = 4096) -> str:
    """Return a ``data:<mime>;base64,<payload>`` URI with a sized payload."""
    return f"data:{mime};base64,{_long_base64(payload_chars)}"


# ---------------------------------------------------------------------------
# Section 1 — pure detection heuristics
# ---------------------------------------------------------------------------


class TestContainsDataUriBase64:
    """The ``data:...;base64,`` URI detector."""

    def test_detects_large_image_data_uri(self) -> None:
        """A real ``data:image/png;base64,<huge>`` URI is detected."""
        text = f"here is the screenshot: {_data_uri('image/png', 8192)}"
        assert contains_data_uri_base64(text) is True

    def test_detects_non_media_data_uri(self) -> None:
        """``data:`` URIs of any media type are detected, not just images.

        Mirrors the hermes-lcm reference comment "Any data URI base64
        payload, not just image/audio/video".
        """
        assert contains_data_uri_base64(_data_uri("application/pdf", 4096)) is True

    def test_tiny_data_uri_below_floor_is_ignored(self) -> None:
        """A ``data:`` URI with a tiny payload is left inline.

        The 256-char payload floor keeps trivially-small inline data URIs
        (a few-byte icon) out of the externalization path.
        """
        tiny = "data:image/png;base64,AB12+/=="
        assert contains_data_uri_base64(tiny) is False

    def test_detects_data_uri_with_json_escaped_slashes(self) -> None:
        """A JSON-escaped ``data:`` URI inside a tool-call string is detected.

        Upstream providers serialize tool-call arguments as JSON, so the
        slash in ``image/png`` arrives as ``\\/``. The regex treats the
        escaped-slash spelling as a slash.
        """
        escaped = "data:image\\/png;base64," + _long_base64(4096)
        assert contains_data_uri_base64(escaped) is True

    def test_plain_text_is_not_a_data_uri(self) -> None:
        """Ordinary prose never trips the data-URI detector."""
        assert contains_data_uri_base64("the quick brown fox " * 200) is False


class TestLooksLikeLongBase64:
    """The conservative long-base64 ratio gate."""

    def test_clean_long_run_matches(self) -> None:
        """A long, well-mixed, pure-base64 run clears the gate."""
        assert looks_like_long_base64(_long_base64(8192)) is True

    def test_short_run_rejected(self) -> None:
        """A run below the 4096-char floor is rejected outright."""
        assert looks_like_long_base64(_long_base64(64)[:64]) is False

    def test_length_residue_one_mod_four_rejected(self) -> None:
        """A run whose length is ``% 4 == 1`` is rejected — invalid base64.

        No valid base64 string has a length residue of 1 mod 4, so such a
        run is almost certainly not a payload.
        """
        run = _long_base64(8192)
        assert len(run) % 4 == 0  # builder guarantees this
        residue_one = run + "A"  # now length % 4 == 1
        assert len(residue_one) % 4 == 1
        assert looks_like_long_base64(residue_one) is False

    def test_low_alphabet_ratio_rejected(self) -> None:
        """A long run with too much non-base64 punctuation is treated as prose."""
        # Inject spaces-as-text every few chars so the ratio drops < 0.98
        # while the whitespace-stripped length still clears the floor.
        noisy = ("ABCD!!!! " * 1200)[:9000]
        assert looks_like_long_base64(noisy) is False

    def test_single_repeated_character_rejected(self) -> None:
        """A 5000-char run of one repeated character is a degenerate log line.

        The >= 8-distinct-character requirement rejects it: a binary
        payload has a mixed alphabet.
        """
        assert looks_like_long_base64("A" * 5000) is False

    def test_jwt_is_not_externalized(self) -> None:
        """A JWT-ish dotted token is below the floor and never matches.

        JWTs are dot-separated, so no single clean run reaches 4096
        characters — the conservative gate leaves them inline.
        """
        jwt = "eyJ" + "a" * 60 + "." + "b" * 120 + "." + "c" * 86
        assert contains_long_base64_run(jwt) is False


class TestContainsLongBase64Run:
    """The whole-text long-base64-run detector."""

    def test_detects_embedded_long_run(self) -> None:
        """A long base64 run embedded in surrounding text is detected."""
        text = f"prefix {_long_base64(8192)} suffix"
        assert contains_long_base64_run(text) is True

    def test_normal_prose_has_no_long_run(self) -> None:
        """A long block of ordinary prose has no clean base64 run."""
        assert contains_long_base64_run("the quick brown fox jumps " * 400) is False

    def test_short_text_is_fast_rejected(self) -> None:
        """Text shorter than the floor is rejected without scanning."""
        assert contains_long_base64_run("short") is False


# ---------------------------------------------------------------------------
# Section 2 — protect_message_for_ingest, LargeFileManager-routed
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path: Path) -> Iterator[LargeFileManager]:
    """A :class:`LargeFileManager` on an in-memory migrated DB.

    These tests do NOT need the full lifecycle DB / sqlite-vec — a bare
    ``run_lcm_migrations`` connection is enough to exercise the
    ``large_files`` table the guard externalizes into.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn)
    conn.execute("INSERT INTO conversations (session_id) VALUES ('guard-sess')")
    conn.commit()
    try:
        yield LargeFileManager(conn, tmp_path / "large-files")
    finally:
        conn.close()


def _conversation_id(manager: LargeFileManager) -> int:
    """Return the single seeded conversation's PK."""
    row = manager._conn.execute(
        "SELECT conversation_id FROM conversations WHERE session_id = 'guard-sess'"
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_data_uri_payload_is_externalized(manager: LargeFileManager) -> None:
    """A ``data:`` base64 URI in message content is externalized.

    The protected content must no longer contain the raw ``data:`` URI;
    it must hold an ``[LCM Raw Payload: ...]`` placeholder; and a
    ``large_files`` row + on-disk blob must exist.
    """
    conv_id = _conversation_id(manager)
    uri = _data_uri("image/png", 8192)
    message = {"role": "user", "content": f"screenshot attached: {uri}"}

    protected = protect_message_for_ingest(message, manager=manager, conversation_id=conv_id)

    content = protected["content"]
    assert isinstance(content, str)
    assert "data:image/png;base64," not in content
    assert "[LCM Raw Payload:" in content
    assert "reason=data_uri_base64" in content

    # The blob landed in the large_files table.
    rows = manager.list_for_conversation(conv_id)
    assert len(rows) == 1
    assert rows[0].mime_type == "image/png"
    # And the externalized blob round-trips byte-for-byte.
    assert manager.read(rows[0].file_id).decode("utf-8") == uri

    # The input message dict was not mutated.
    assert message["content"] == f"screenshot attached: {uri}"


def test_long_base64_run_is_externalized(manager: LargeFileManager) -> None:
    """A long stand-alone base64 run (no ``data:`` prefix) is externalized."""
    conv_id = _conversation_id(manager)
    run = _long_base64(8192)
    message = {"role": "tool", "content": f"raw blob: {run} done"}

    protected = protect_message_for_ingest(message, manager=manager, conversation_id=conv_id)

    content = protected["content"]
    assert isinstance(content, str)
    assert run not in content
    assert "[LCM Raw Payload:" in content
    assert "reason=inline_base64_run" in content

    rows = manager.list_for_conversation(conv_id)
    assert len(rows) == 1
    assert manager.read(rows[0].file_id).decode("utf-8") == run


def test_normal_text_is_untouched(manager: LargeFileManager) -> None:
    """A normal-text message passes through with no externalization.

    The guard is a no-op in the common case: the returned content equals
    the input and no ``large_files`` row is created.
    """
    conv_id = _conversation_id(manager)
    message = {"role": "user", "content": "Just a normal question about the code."}

    protected = protect_message_for_ingest(message, manager=manager, conversation_id=conv_id)

    assert protected["content"] == "Just a normal question about the code."
    assert manager.list_for_conversation(conv_id) == []


def test_conservative_ratio_gate_keeps_jwt_inline(manager: LargeFileManager) -> None:
    """A JWT-ish token below the gate is NOT externalized.

    Exercises the conservative-ratio gate end-to-end: a dotted JWT has no
    single 4096-char clean run, so the guard leaves it inline and creates
    no ``large_files`` row.
    """
    conv_id = _conversation_id(manager)
    jwt = "eyJ" + "a" * 200 + "." + "b" * 300 + "." + "c" * 400
    message = {"role": "assistant", "content": f"your token is {jwt}"}

    protected = protect_message_for_ingest(message, manager=manager, conversation_id=conv_id)

    assert protected["content"] == f"your token is {jwt}"
    assert manager.list_for_conversation(conv_id) == []


def test_idempotent_reprotect_is_noop(manager: LargeFileManager) -> None:
    """Re-protecting an already-protected message externalizes nothing new.

    The ``[LCM Raw Payload: ...]`` placeholder is plain text with no
    ``data:`` URI and no long base64 run, so a second guard pass finds
    nothing — exactly one ``large_files`` row exists after two passes.
    """
    conv_id = _conversation_id(manager)
    message = {"role": "user", "content": f"img: {_data_uri('image/jpeg', 8192)}"}

    first = protect_message_for_ingest(message, manager=manager, conversation_id=conv_id)
    assert "[LCM Raw Payload:" in first["content"]
    assert len(manager.list_for_conversation(conv_id)) == 1

    # Second pass over the already-protected message.
    second = protect_message_for_ingest(first, manager=manager, conversation_id=conv_id)
    # Content is unchanged and no new blob was written.
    assert second["content"] == first["content"]
    assert len(manager.list_for_conversation(conv_id)) == 1


def test_payload_nested_in_content_block_is_externalized(
    manager: LargeFileManager,
) -> None:
    """A base64 payload nested in an Anthropic-style content block is reached.

    The guard walks dicts/lists so a payload inside
    ``{"type": "image", "source": {"data": "<base64>"}}`` is externalized
    even though it is not a top-level string.
    """
    conv_id = _conversation_id(manager)
    payload = _long_base64(8192)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {"type": "base64", "data": payload}},
        ],
    }

    protected = protect_message_for_ingest(message, manager=manager, conversation_id=conv_id)

    block = protected["content"][1]
    assert payload not in block["source"]["data"]
    assert "[LCM Raw Payload:" in block["source"]["data"]
    # The sibling text block is untouched.
    assert protected["content"][0]["text"] == "look at this"
    assert len(manager.list_for_conversation(conv_id)) == 1


def test_tool_calls_payload_is_externalized(manager: LargeFileManager) -> None:
    """A base64 payload inside ``message["tool_calls"]`` is externalized."""
    conv_id = _conversation_id(manager)
    payload = _long_base64(8192)
    message = {
        "role": "assistant",
        "content": "calling a tool",
        "tool_calls": [{"id": "t1", "arguments": {"blob": payload}}],
    }

    protected = protect_message_for_ingest(message, manager=manager, conversation_id=conv_id)

    arg_blob = protected["tool_calls"][0]["arguments"]["blob"]
    assert payload not in arg_blob
    assert "[LCM Raw Payload:" in arg_blob
    assert len(manager.list_for_conversation(conv_id)) == 1


def test_externalization_failure_preserves_inline_content(
    manager: LargeFileManager,
) -> None:
    """A failed externalization leaves the inline payload untouched.

    Non-blocking contract: losslessness is never sacrificed. When
    :meth:`LargeFileManager.externalize_block` raises, the guard logs and
    keeps the raw payload inline rather than dropping it.
    """
    conv_id = _conversation_id(manager)
    uri = _data_uri("image/png", 8192)
    message = {"role": "user", "content": f"shot: {uri}"}

    def _boom(**_kwargs: object) -> object:
        raise OSError("simulated disk-full")

    # Patch the manager's externalize path to fail.
    object.__setattr__(manager, "externalize_block", _boom)

    protected = protect_message_for_ingest(message, manager=manager, conversation_id=conv_id)

    # The raw payload is preserved — no data loss.
    assert protected["content"] == f"shot: {uri}"


# ---------------------------------------------------------------------------
# Section 3 — end-to-end: guard wired into _ingest_single
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_home: Path) -> Iterator[LCMEngine]:
    """An :class:`LCMEngine` with ``on_session_start`` already run."""
    eng = LCMEngine(hermes_home=tmp_home / ".hermes", config=LcmConfig())
    eng.on_session_start("guard-e2e")
    try:
        yield eng
    finally:
        eng.on_session_end("guard-e2e", [])


@_skip_no_extension_loading
def test_data_uri_message_lands_as_placeholder_in_db(engine: LCMEngine) -> None:
    """End-to-end: a ``data:`` base64 message is stored as a placeholder.

    Ingest a message carrying a large ``data:image/png;base64,`` URI
    through the real ``post_llm_call`` path. The persisted
    ``messages.content`` must hold the compact placeholder, NOT the raw
    payload — and a ``large_files`` row must exist.
    """
    uri = _data_uri("image/png", 8192)
    engine._on_post_llm_call(
        session_id="sess-e2e",
        conversation_history=[{"role": "user", "content": f"see: {uri}"}],
    )

    conv = engine._conversation_store.get_conversation_by_session_id("sess-e2e")
    assert conv is not None
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert len(msgs) == 1

    stored = msgs[0].content
    assert "data:image/png;base64," not in stored
    assert "[LCM Raw Payload:" in stored

    # The blob was externalized into large_files.
    assert engine._db is not None
    count = engine._db.execute("SELECT COUNT(*) FROM large_files").fetchone()[0]
    assert count == 1


@_skip_no_extension_loading
def test_data_uri_does_not_pollute_fts_shadow(engine: LCMEngine) -> None:
    """End-to-end: the raw base64 payload never reaches the ``messages_fts`` shadow.

    ``ConversationStore.create_message`` indexes ``content`` into
    ``messages_fts`` in the same transaction. Because the guard runs
    BEFORE that write, the FTS shadow stores the placeholder — searching
    the FTS table for the raw payload returns nothing.
    """
    payload_run = _long_base64(8192)
    uri = f"data:image/png;base64,{payload_run}"
    engine._on_post_llm_call(
        session_id="sess-fts",
        conversation_history=[{"role": "user", "content": f"x {uri} y"}],
    )

    assert engine._db is not None
    # The FTS shadow row holds the placeholder, not the raw payload.
    fts_rows = engine._db.execute("SELECT content FROM messages_fts").fetchall()
    assert len(fts_rows) == 1
    assert payload_run not in fts_rows[0][0]
    assert "[LCM Raw Payload:" in fts_rows[0][0]


@_skip_no_extension_loading
def test_normal_message_ingests_unchanged_end_to_end(engine: LCMEngine) -> None:
    """End-to-end: a normal-text turn ingests with no externalization.

    The guard must not perturb the common case — content is stored
    verbatim and no ``large_files`` row is created.
    """
    engine._on_post_llm_call(
        session_id="sess-normal",
        conversation_history=[
            {"role": "user", "content": "what does this function do?"},
            {"role": "assistant", "content": "it sorts the list."},
        ],
    )

    conv = engine._conversation_store.get_conversation_by_session_id("sess-normal")
    assert conv is not None
    msgs = engine._conversation_store.get_messages(conv.conversation_id)
    assert [m.content for m in msgs] == [
        "what does this function do?",
        "it sorts the list.",
    ]

    assert engine._db is not None
    count = engine._db.execute("SELECT COUNT(*) FROM large_files").fetchone()[0]
    assert count == 0


@_skip_no_extension_loading
def test_idempotent_reingest_after_restart_no_duplicate_blobs(
    engine: LCMEngine,
) -> None:
    """End-to-end idempotency: re-ingesting a protected transcript adds no blobs.

    Ingest a ``data:`` URI message, then replay the SAME
    ``conversation_history`` on a fresh hook fire. The diff cursor makes
    the second fire a no-op; even if it were not, the stored placeholder
    holds no ``data:`` URI so a re-guard externalizes nothing. Exactly one
    ``large_files`` row exists after both fires.
    """
    uri = _data_uri("image/png", 8192)
    history = [{"role": "user", "content": f"img: {uri}"}]

    engine._on_post_llm_call(session_id="sess-idem", conversation_history=history)
    assert engine._db is not None
    first_count = engine._db.execute("SELECT COUNT(*) FROM large_files").fetchone()[0]
    assert first_count == 1

    # Replay the identical history — diff cursor short-circuits.
    engine._on_post_llm_call(session_id="sess-idem", conversation_history=history)
    second_count = engine._db.execute("SELECT COUNT(*) FROM large_files").fetchone()[0]
    assert second_count == 1
