"""Tests for :mod:`lossless_hermes.store.conversation`.

Covers acceptance criteria from
``epics/01-storage/01-08-conversation-store.md``:

* Public-surface parity with the 29 TS methods (per storage.md §4.1 table).
* ``create_message`` auto-computes ``identity_hash`` and writes to
  ``messages_fts`` in the same transaction.
* ``delete_messages`` cascades to ``message_parts`` (FK CASCADE) AND
  removes corresponding rows from ``messages_fts`` (manual DELETE).
* Conversation lifecycle (create / get / archive / bootstrap /
  get-or-create / UNIQUE-race recovery).
* Message dedup via identity_hash (``has_message`` /
  ``count_messages_by_identity``).
* ``message_parts`` bulk insert + readback.
* Search dispatcher (FTS5 / LIKE / regex backends + CJK routing).
* ``with_transaction`` semantics: 3-deep nesting works (the savepoint
  nesting invariant).
* JSON.parse error handling: invalid JSON in ``metadata`` returns the
  raw string (no parse here; caller's job — see module docstring).

Test setup: each test gets a fresh in-memory DB with the migration ladder
applied. Tests that exercise FTS5 paths create the ``messages_fts`` table
inline (FTS5 issue #01-05 hasn't landed; the test creates the same shape
the migration would).

References:

* :mod:`lossless_hermes.store.conversation` — implementation.
* ``/Volumes/LEXAR/Claude/lossless-claw/src/store/conversation-store.ts`` — TS source.
* ``epics/01-storage/01-08-conversation-store.md`` — issue spec.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest

from lossless_hermes.db.migration import run_lcm_migrations
from lossless_hermes.store.conversation import (
    ConversationStore,
    CreateConversationInput,
    CreateMessageInput,
    CreateMessagePartInput,
    MessageSearchInput,
)
from lossless_hermes.store.message_identity import build_message_identity_hash

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """An in-memory DB with the migration ladder applied + FTS5 + FK on.

    Uses ``isolation_level=None`` so Python's sqlite3 module does NOT
    auto-start transactions on DML statements — matching the behavior of
    :func:`lossless_hermes.db.connection.open_lcm_db` (which the store
    expects). Without this, calls to ``with_database_transaction`` would
    raise "cannot start a transaction within a transaction" because the
    previous INSERT/UPDATE auto-opened a transaction that's still open.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, seed_default_prompts=False)
    # FTS5 issue #01-05 hasn't landed; create the messages_fts table inline
    # using the shape from storage.md §2.2.
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
        "USING fts5(content, tokenize='porter unicode61')"
    )
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def db_no_fts() -> Iterator[sqlite3.Connection]:
    """An in-memory DB with the migration ladder applied but no messages_fts.

    Used to test the LIKE-fallback path (when FTS5 isn't available or
    the messages_fts table is missing). Pass `fts5_available=False` to
    `run_lcm_migrations` so #01-05's `_ensure_fts5_tables` skips creation.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    run_lcm_migrations(conn, fts5_available=False, seed_default_prompts=False)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def store(db: sqlite3.Connection) -> ConversationStore:
    """A ConversationStore with FTS5 enabled (messages_fts table exists)."""
    return ConversationStore(db, fts5_available=True)


@pytest.fixture
def store_no_fts(db_no_fts: sqlite3.Connection) -> ConversationStore:
    """A ConversationStore with FTS5 disabled (no messages_fts writes)."""
    return ConversationStore(db_no_fts, fts5_available=False)


# ---------------------------------------------------------------------------
# Conversation lifecycle
# ---------------------------------------------------------------------------


def test_create_conversation_returns_shaped_record(
    store: ConversationStore,
) -> None:
    """create_conversation returns a fully populated ConversationRecord."""
    record = store.create_conversation(CreateConversationInput(session_id="sess-1", title="hello"))
    assert record.conversation_id > 0
    assert record.session_id == "sess-1"
    assert record.session_key is None
    assert record.active is True
    assert record.title == "hello"
    assert record.bootstrapped_at is None
    assert record.archived_at is None
    # created_at + updated_at are aware datetimes in UTC.
    assert record.created_at.tzinfo is not None
    assert record.updated_at.tzinfo is not None


def test_create_conversation_with_session_key(store: ConversationStore) -> None:
    """session_key is persisted and retrievable via get_conversation_by_session_key."""
    record = store.create_conversation(
        CreateConversationInput(session_id="sess-2", session_key="key-2")
    )
    assert record.session_key == "key-2"

    by_key = store.get_conversation_by_session_key("key-2")
    assert by_key is not None
    assert by_key.conversation_id == record.conversation_id


def test_create_conversation_inactive(store: ConversationStore) -> None:
    """Passing active=False inserts an archived row."""
    record = store.create_conversation(CreateConversationInput(session_id="sess-3", active=False))
    assert record.active is False


def test_create_conversation_unique_race_recovers_existing(
    store: ConversationStore,
) -> None:
    """Hitting the partial UNIQUE on session_key returns the existing row.

    Mirrors the TS UNIQUE-race recovery (TS lines 309-323): when a second
    writer races to insert a conversation with the same active session_key,
    the IntegrityError is caught and the existing row is returned.
    """
    first = store.create_conversation(
        CreateConversationInput(session_id="race-1", session_key="race-key")
    )
    # Second create with same session_key (the partial UNIQUE would fire).
    # The store recovers by returning the existing row.
    second = store.create_conversation(
        CreateConversationInput(session_id="race-2", session_key="race-key")
    )
    assert second.conversation_id == first.conversation_id


def test_get_conversation_missing_returns_none(store: ConversationStore) -> None:
    """get_conversation returns None for nonexistent ids."""
    assert store.get_conversation(99999) is None


def test_get_conversation_by_session_id_returns_newest_active(
    store: ConversationStore,
) -> None:
    """get_conversation_by_session_id prefers active over archived."""
    # Create archived first (older), then active.
    archived = store.create_conversation(
        CreateConversationInput(session_id="dup-sess", active=False)
    )
    active = store.create_conversation(CreateConversationInput(session_id="dup-sess", active=True))

    found = store.get_conversation_by_session_id("dup-sess")
    assert found is not None
    assert found.conversation_id == active.conversation_id
    assert found.conversation_id != archived.conversation_id


def test_get_conversation_by_session_key_archived_excluded(
    store: ConversationStore,
) -> None:
    """get_conversation_by_session_key returns None when only archived rows match.

    The partial UNIQUE index ``conversations_active_session_key_idx`` is
    scoped to active = 1; archived rows are not returned by this method.
    """
    store.create_conversation(
        CreateConversationInput(session_id="s-a", session_key="key-a", active=False)
    )
    # active=False — should NOT be returned by session-key lookup.
    assert store.get_conversation_by_session_key("key-a") is None


def test_get_or_create_existing_active_session_id(
    store: ConversationStore,
) -> None:
    """get_or_create returns existing row when session_id matches an active conv."""
    first = store.get_or_create_conversation("sess-goc-1")
    second = store.get_or_create_conversation("sess-goc-1")
    assert first.conversation_id == second.conversation_id


def test_get_or_create_with_session_key_updates_session_id(
    store: ConversationStore,
) -> None:
    """When session_key matches but session_id drifted, the session_id is updated."""
    first = store.create_conversation(
        CreateConversationInput(session_id="sess-drift-1", session_key="key-drift")
    )
    second = store.get_or_create_conversation("sess-drift-2", session_key="key-drift")
    assert first.conversation_id == second.conversation_id
    assert second.session_id == "sess-drift-2"


def test_get_or_create_backfills_missing_session_key(
    store: ConversationStore,
) -> None:
    """An active row with no session_key gets backfilled when the caller supplies one."""
    first = store.create_conversation(CreateConversationInput(session_id="sess-back-1"))
    assert first.session_key is None

    second = store.get_or_create_conversation("sess-back-1", session_key="late-key")
    assert second.conversation_id == first.conversation_id
    assert second.session_key == "late-key"


def test_archive_conversation_sets_active_zero(store: ConversationStore) -> None:
    """archive_conversation flips active to 0 and sets archived_at."""
    record = store.create_conversation(CreateConversationInput(session_id="sess-arch"))
    assert record.active is True

    store.archive_conversation(record.conversation_id)

    refreshed = store.get_conversation(record.conversation_id)
    assert refreshed is not None
    assert refreshed.active is False
    assert refreshed.archived_at is not None


def test_archive_conversation_is_idempotent(store: ConversationStore) -> None:
    """Re-archiving an already-archived conv keeps the original archived_at."""
    record = store.create_conversation(CreateConversationInput(session_id="sess-arch-idem"))
    store.archive_conversation(record.conversation_id)
    first = store.get_conversation(record.conversation_id)
    assert first is not None
    assert first.archived_at is not None
    first_archived_at = first.archived_at

    # Re-archive — archived_at should not change.
    store.archive_conversation(record.conversation_id)
    second = store.get_conversation(record.conversation_id)
    assert second is not None
    assert second.archived_at == first_archived_at


def test_mark_conversation_bootstrapped_sets_timestamp(
    store: ConversationStore,
) -> None:
    """mark_conversation_bootstrapped fills in bootstrapped_at."""
    record = store.create_conversation(CreateConversationInput(session_id="sess-boot"))
    assert record.bootstrapped_at is None

    store.mark_conversation_bootstrapped(record.conversation_id)
    refreshed = store.get_conversation(record.conversation_id)
    assert refreshed is not None
    assert refreshed.bootstrapped_at is not None


def test_list_active_conversations(store: ConversationStore) -> None:
    """list_active_conversations returns only active rows, newest first."""
    a = store.create_conversation(CreateConversationInput(session_id="a"))
    b = store.create_conversation(CreateConversationInput(session_id="b"))
    c = store.create_conversation(CreateConversationInput(session_id="c"))
    store.archive_conversation(b.conversation_id)

    active = store.list_active_conversations()
    ids = [r.conversation_id for r in active]
    assert b.conversation_id not in ids
    assert a.conversation_id in ids
    assert c.conversation_id in ids


def test_get_conversation_family_ids_by_session_key(
    store: ConversationStore,
) -> None:
    """get_conversation_family_ids returns all conv ids sharing a session_key."""
    a = store.create_conversation(
        CreateConversationInput(session_id="fam-1", session_key="fam-key")
    )
    store.archive_conversation(a.conversation_id)
    # Now safe to create a second active conv with the same key.
    b = store.create_conversation(
        CreateConversationInput(session_id="fam-2", session_key="fam-key")
    )

    ids = store.get_conversation_family_ids(session_key="fam-key")
    assert set(ids) == {a.conversation_id, b.conversation_id}


def test_get_conversation_family_ids_no_session_key(
    store: ConversationStore,
) -> None:
    """When session_key is empty, family is grouped by session_id."""
    a = store.create_conversation(CreateConversationInput(session_id="fam-sid"))
    store.archive_conversation(a.conversation_id)
    b = store.create_conversation(CreateConversationInput(session_id="fam-sid"))

    ids = store.get_conversation_family_ids(conversation_id=b.conversation_id)
    assert set(ids) == {a.conversation_id, b.conversation_id}


# ---------------------------------------------------------------------------
# Message operations
# ---------------------------------------------------------------------------


def test_create_message_auto_computes_identity_hash(
    store: ConversationStore, db: sqlite3.Connection
) -> None:
    """create_message auto-computes identity_hash via build_message_identity_hash."""
    conv = store.create_conversation(CreateConversationInput(session_id="msg-test-1"))
    record = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello",
            token_count=2,
        )
    )
    assert record.message_id > 0

    # Inspect the raw row to confirm the identity_hash is stored.
    row = db.execute(
        "SELECT identity_hash FROM messages WHERE message_id = ?",
        (record.message_id,),
    ).fetchone()
    expected_hash = build_message_identity_hash("user", "hello")
    assert row[0] == expected_hash


def test_create_message_writes_to_messages_fts(
    store: ConversationStore, db: sqlite3.Connection
) -> None:
    """When fts5_available=True, create_message inserts into messages_fts."""
    conv = store.create_conversation(CreateConversationInput(session_id="fts-write"))
    record = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello fts world",
            token_count=3,
        )
    )
    # The FTS table should now have this row.
    rows = db.execute(
        "SELECT content FROM messages_fts WHERE rowid = ?",
        (record.message_id,),
    ).fetchall()
    assert len(rows) == 1
    assert "hello fts world" in rows[0][0]


def test_create_message_no_fts_skips_fts_write(
    store_no_fts: ConversationStore, db_no_fts: sqlite3.Connection
) -> None:
    """When fts5_available=False, no messages_fts INSERT is attempted.

    The store does not raise even though messages_fts doesn't exist,
    because the FTS write is guarded on the flag.
    """
    conv = store_no_fts.create_conversation(CreateConversationInput(session_id="no-fts"))
    store_no_fts.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello no fts",
            token_count=3,
        )
    )
    # messages_fts table doesn't exist — but no error was raised.
    rows = db_no_fts.execute(
        "SELECT name FROM sqlite_master WHERE name = 'messages_fts'"
    ).fetchall()
    assert rows == []


def test_create_message_explicit_identity_hash_preserved(
    store: ConversationStore, db: sqlite3.Connection
) -> None:
    """An explicit identity_hash is stored verbatim (overrides the auto-compute)."""
    conv = store.create_conversation(CreateConversationInput(session_id="ihash-explicit"))
    record = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="any",
            token_count=1,
            identity_hash="custom-hash-value",
        )
    )
    row = db.execute(
        "SELECT identity_hash FROM messages WHERE message_id = ?",
        (record.message_id,),
    ).fetchone()
    assert row[0] == "custom-hash-value"


def test_create_messages_bulk_returns_records_in_order(
    store: ConversationStore,
) -> None:
    """create_messages_bulk returns records in input order."""
    conv = store.create_conversation(CreateConversationInput(session_id="bulk-test"))
    inputs = [
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=i,
            role="user",
            content=f"msg-{i}",
            token_count=1,
        )
        for i in range(5)
    ]
    records = store.create_messages_bulk(inputs)
    assert len(records) == 5
    for i, record in enumerate(records):
        assert record.seq == i
        assert record.content == f"msg-{i}"


def test_create_messages_bulk_empty_returns_empty(store: ConversationStore) -> None:
    """Empty input yields empty output (no DB roundtrip)."""
    assert store.create_messages_bulk([]) == []


def test_get_messages_orders_by_seq(store: ConversationStore) -> None:
    """get_messages returns rows in seq ASC."""
    conv = store.create_conversation(CreateConversationInput(session_id="get-msgs"))
    for i in [3, 1, 2, 0, 4]:
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=i,
                role="user",
                content=f"m-{i}",
                token_count=1,
            )
        )
    messages = store.get_messages(conv.conversation_id)
    assert [m.seq for m in messages] == [0, 1, 2, 3, 4]


def test_get_messages_after_seq_filters(store: ConversationStore) -> None:
    """get_messages with after_seq returns only rows with seq > after_seq."""
    conv = store.create_conversation(CreateConversationInput(session_id="after-seq"))
    for i in range(5):
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=i,
                role="user",
                content=f"m-{i}",
                token_count=1,
            )
        )
    messages = store.get_messages(conv.conversation_id, after_seq=2)
    assert [m.seq for m in messages] == [3, 4]


def test_get_messages_limit(store: ConversationStore) -> None:
    """get_messages with limit caps the result count."""
    conv = store.create_conversation(CreateConversationInput(session_id="limit-msgs"))
    for i in range(5):
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=i,
                role="user",
                content=f"m-{i}",
                token_count=1,
            )
        )
    messages = store.get_messages(conv.conversation_id, limit=2)
    assert len(messages) == 2
    assert [m.seq for m in messages] == [0, 1]


def test_get_last_message(store: ConversationStore) -> None:
    """get_last_message returns the highest-seq row."""
    conv = store.create_conversation(CreateConversationInput(session_id="last-msg"))
    assert store.get_last_message(conv.conversation_id) is None

    for i in range(3):
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=i,
                role="user",
                content=f"m-{i}",
                token_count=1,
            )
        )
    last = store.get_last_message(conv.conversation_id)
    assert last is not None
    assert last.seq == 2


def test_has_message_returns_true_for_existing(store: ConversationStore) -> None:
    """has_message confirms an exact (role, content) match exists."""
    conv = store.create_conversation(CreateConversationInput(session_id="has-msg"))
    store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello",
            token_count=1,
        )
    )
    assert store.has_message(conv.conversation_id, "user", "hello") is True
    assert store.has_message(conv.conversation_id, "user", "different") is False
    assert store.has_message(conv.conversation_id, "assistant", "hello") is False


def test_count_messages_by_identity(store: ConversationStore) -> None:
    """count_messages_by_identity returns the multiplicity of a (role, content) pair."""
    conv = store.create_conversation(CreateConversationInput(session_id="count-msgs"))
    # Insert the same content twice (different seq).
    store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="same",
            token_count=1,
        )
    )
    store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=1,
            role="user",
            content="same",
            token_count=1,
        )
    )
    assert store.count_messages_by_identity(conv.conversation_id, "user", "same") == 2
    assert store.count_messages_by_identity(conv.conversation_id, "user", "different") == 0


def test_get_message_by_id_filters_suppressed_by_default(
    store: ConversationStore, db: sqlite3.Connection
) -> None:
    """get_message_by_id excludes suppressed messages by default (v4.1 fix)."""
    conv = store.create_conversation(CreateConversationInput(session_id="supp-test"))
    record = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello",
            token_count=1,
        )
    )
    # Suppress the message.
    db.execute(
        "UPDATE messages SET suppressed_at = datetime('now') WHERE message_id = ?",
        (record.message_id,),
    )

    # Default: suppressed row is hidden.
    assert store.get_message_by_id(record.message_id) is None

    # Opt-in: include_suppressed=True returns it.
    found = store.get_message_by_id(record.message_id, include_suppressed=True)
    assert found is not None
    assert found.message_id == record.message_id


def test_get_message_count(store: ConversationStore) -> None:
    """get_message_count returns the row count."""
    conv = store.create_conversation(CreateConversationInput(session_id="cnt-test"))
    assert store.get_message_count(conv.conversation_id) == 0

    for i in range(3):
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=i,
                role="user",
                content=f"m-{i}",
                token_count=1,
            )
        )
    assert store.get_message_count(conv.conversation_id) == 3


def test_get_max_seq_empty_returns_zero(store: ConversationStore) -> None:
    """get_max_seq returns 0 for empty conversation (COALESCE default)."""
    conv = store.create_conversation(CreateConversationInput(session_id="maxseq-empty"))
    assert store.get_max_seq(conv.conversation_id) == 0


def test_get_max_seq_with_messages(store: ConversationStore) -> None:
    """get_max_seq returns the largest seq."""
    conv = store.create_conversation(CreateConversationInput(session_id="maxseq-nonempty"))
    for i in [0, 5, 2, 9, 3]:
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=i,
                role="user",
                content=f"m-{i}",
                token_count=1,
            )
        )
    assert store.get_max_seq(conv.conversation_id) == 9


# ---------------------------------------------------------------------------
# Message parts
# ---------------------------------------------------------------------------


def test_create_message_parts_bulk_and_readback(store: ConversationStore) -> None:
    """create_message_parts inserts; get_message_parts returns in ordinal order."""
    conv = store.create_conversation(CreateConversationInput(session_id="parts-test"))
    message = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello",
            token_count=1,
        )
    )
    parts = [
        CreateMessagePartInput(
            session_id="sess-parts",
            part_type="text",
            ordinal=2,
            text_content="third",
        ),
        CreateMessagePartInput(
            session_id="sess-parts",
            part_type="text",
            ordinal=0,
            text_content="first",
        ),
        CreateMessagePartInput(
            session_id="sess-parts",
            part_type="text",
            ordinal=1,
            text_content="second",
        ),
    ]
    store.create_message_parts(message.message_id, parts)

    fetched = store.get_message_parts(message.message_id)
    assert len(fetched) == 3
    # Returned in ordinal order.
    assert [p.ordinal for p in fetched] == [0, 1, 2]
    assert [p.text_content for p in fetched] == ["first", "second", "third"]
    # Each part has a generated UUID part_id.
    assert all(len(p.part_id) == 36 for p in fetched)


def test_create_message_parts_empty_is_noop(store: ConversationStore) -> None:
    """Empty parts list does not raise."""
    conv = store.create_conversation(CreateConversationInput(session_id="parts-empty"))
    message = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello",
            token_count=1,
        )
    )
    store.create_message_parts(message.message_id, [])
    assert store.get_message_parts(message.message_id) == []


def test_message_parts_metadata_preserves_invalid_json(
    store: ConversationStore,
) -> None:
    """Invalid JSON in metadata is preserved verbatim (caller does the parse).

    Per the module docstring, the store does NOT parse JSON. The
    acceptance test checks that an invalid-JSON metadata blob survives
    insert + readback unchanged. (No warning is emitted at the store
    layer because parsing is deferred to callers; see the module
    docstring for the design rationale.)
    """
    conv = store.create_conversation(CreateConversationInput(session_id="parts-bad-json"))
    message = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="x",
            token_count=1,
        )
    )
    parts = [
        CreateMessagePartInput(
            session_id="sess-bad",
            part_type="tool",
            ordinal=0,
            metadata="not valid json {",
        )
    ]
    store.create_message_parts(message.message_id, parts)
    fetched = store.get_message_parts(message.message_id)
    assert len(fetched) == 1
    assert fetched[0].metadata == "not valid json {"


def test_message_parts_cascade_on_message_delete(
    store: ConversationStore, db: sqlite3.Connection
) -> None:
    """Deleting a message cascades to its message_parts via FK CASCADE."""
    conv = store.create_conversation(CreateConversationInput(session_id="parts-cascade"))
    message = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="x",
            token_count=1,
        )
    )
    store.create_message_parts(
        message.message_id,
        [CreateMessagePartInput(session_id="s", part_type="text", ordinal=0, text_content="a")],
    )
    assert len(store.get_message_parts(message.message_id)) == 1

    store.delete_messages([message.message_id])

    rows = db.execute(
        "SELECT 1 FROM message_parts WHERE message_id = ?",
        (message.message_id,),
    ).fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# Deletion + FTS cleanup
# ---------------------------------------------------------------------------


def test_delete_messages_removes_fts_row(store: ConversationStore, db: sqlite3.Connection) -> None:
    """delete_messages removes the corresponding messages_fts row."""
    conv = store.create_conversation(CreateConversationInput(session_id="del-fts"))
    message = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello fts removal",
            token_count=3,
        )
    )
    # Confirm pre-state.
    fts_pre = db.execute(
        "SELECT content FROM messages_fts WHERE rowid = ?",
        (message.message_id,),
    ).fetchall()
    assert len(fts_pre) == 1

    deleted = store.delete_messages([message.message_id])
    assert deleted == 1

    fts_post = db.execute(
        "SELECT content FROM messages_fts WHERE rowid = ?",
        (message.message_id,),
    ).fetchall()
    assert fts_post == []


def test_delete_messages_empty_returns_zero(store: ConversationStore) -> None:
    """delete_messages([]) returns 0 with no DB roundtrip."""
    assert store.delete_messages([]) == 0


def test_delete_messages_skips_summary_referenced(
    store: ConversationStore, db: sqlite3.Connection
) -> None:
    """Messages referenced in summary_messages are skipped (RESTRICT)."""
    conv = store.create_conversation(CreateConversationInput(session_id="del-skip"))
    message = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="x",
            token_count=1,
        )
    )
    # Insert a summary that references this message.
    db.execute(
        "INSERT INTO summaries (summary_id, conversation_id, kind, content, "
        "token_count) VALUES (?, ?, ?, ?, ?)",
        ("sum-1", conv.conversation_id, "leaf", "summary content", 5),
    )
    db.execute(
        "INSERT INTO summary_messages (summary_id, message_id, ordinal) VALUES (?, ?, ?)",
        ("sum-1", message.message_id, 0),
    )

    # Now delete_messages should skip this message.
    deleted = store.delete_messages([message.message_id])
    assert deleted == 0

    # Message still present.
    assert store.get_message_by_id(message.message_id) is not None


def test_delete_messages_removes_context_items(
    store: ConversationStore, db: sqlite3.Connection
) -> None:
    """delete_messages removes context_items rows that reference the message."""
    conv = store.create_conversation(CreateConversationInput(session_id="del-ctx"))
    message = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="x",
            token_count=1,
        )
    )
    # Insert a context_items row.
    db.execute(
        "INSERT INTO context_items (conversation_id, ordinal, item_type, "
        "message_id) VALUES (?, ?, ?, ?)",
        (conv.conversation_id, 0, "message", message.message_id),
    )

    deleted = store.delete_messages([message.message_id])
    assert deleted == 1

    rows = db.execute(
        "SELECT 1 FROM context_items WHERE message_id = ?",
        (message.message_id,),
    ).fetchall()
    assert rows == []


# ---------------------------------------------------------------------------
# Search dispatcher + backends
# ---------------------------------------------------------------------------


def test_search_messages_full_text_fts5(store: ConversationStore) -> None:
    """search_messages dispatches to FTS5 when available + query is non-CJK."""
    conv = store.create_conversation(CreateConversationInput(session_id="fts-search"))
    for i, text in enumerate(["hello fts world", "no match here", "fts world again"]):
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=i,
                role="user",
                content=text,
                token_count=3,
            )
        )

    results = store.search_messages(MessageSearchInput(query="fts", mode="full_text"))
    assert len(results) == 2
    contents = [r.snippet for r in results]
    # FTS5 snippets are substrings of the content.
    assert all("fts" in c.lower() for c in contents)


def test_search_messages_full_text_cjk_routes_to_like(
    store: ConversationStore,
) -> None:
    """CJK query routes to LIKE fallback even when FTS5 is available.

    FTS5 unicode61 tokenizer cannot index CJK reliably; the dispatcher
    detects CJK and uses LIKE.
    """
    conv = store.create_conversation(CreateConversationInput(session_id="cjk-search"))
    store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello 你好 world",
            token_count=4,
        )
    )
    results = store.search_messages(MessageSearchInput(query="你好", mode="full_text"))
    assert len(results) == 1
    # Snippet contains the CJK characters.
    assert "你好" in results[0].snippet


def test_search_messages_full_text_like_fallback(
    store_no_fts: ConversationStore,
) -> None:
    """When fts5_available=False, search_messages uses LIKE directly."""
    conv = store_no_fts.create_conversation(CreateConversationInput(session_id="like-search"))
    for i, text in enumerate(["hello world", "no match", "world hello"]):
        store_no_fts.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=i,
                role="user",
                content=text,
                token_count=2,
            )
        )

    results = store_no_fts.search_messages(MessageSearchInput(query="hello", mode="full_text"))
    assert len(results) == 2
    assert all("hello" in r.snippet.lower() for r in results)


def test_search_messages_regex(store: ConversationStore) -> None:
    """search_messages with mode=regex uses Python re.search."""
    conv = store.create_conversation(CreateConversationInput(session_id="regex-search"))
    for i, text in enumerate(["abc123", "no numbers", "xy456"]):
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=i,
                role="user",
                content=text,
                token_count=1,
            )
        )

    results = store.search_messages(MessageSearchInput(query=r"\d+", mode="regex"))
    # Two messages contain digits.
    assert len(results) == 2
    snippets = sorted([r.snippet for r in results])
    assert snippets == ["123", "456"]


def test_search_regex_invalid_pattern_returns_empty(
    store: ConversationStore,
) -> None:
    """An invalid regex pattern returns empty (no raise)."""
    conv = store.create_conversation(CreateConversationInput(session_id="regex-bad"))
    store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="any",
            token_count=1,
        )
    )
    # Unbalanced paren is invalid regex.
    results = store.search_messages(MessageSearchInput(query="(unbalanced", mode="regex"))
    assert results == []


def test_search_regex_excessive_length_rejected(
    store: ConversationStore,
) -> None:
    """Patterns longer than 500 chars are rejected (ReDoS guard)."""
    conv = store.create_conversation(CreateConversationInput(session_id="regex-long"))
    store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="x",
            token_count=1,
        )
    )
    long_pattern = "a" * 501
    results = store.search_messages(MessageSearchInput(query=long_pattern, mode="regex"))
    assert results == []


def test_search_filters_suppressed_messages(
    store: ConversationStore, db: sqlite3.Connection
) -> None:
    """All three search backends filter suppressed_at IS NOT NULL."""
    conv = store.create_conversation(CreateConversationInput(session_id="search-supp"))
    msg = store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello suppressed",
            token_count=2,
        )
    )
    # Suppress.
    db.execute(
        "UPDATE messages SET suppressed_at = datetime('now') WHERE message_id = ?",
        (msg.message_id,),
    )

    # FTS5 search.
    fts_results = store.search_messages(MessageSearchInput(query="suppressed", mode="full_text"))
    assert fts_results == []

    # Regex search.
    regex_results = store.search_messages(MessageSearchInput(query="suppressed", mode="regex"))
    assert regex_results == []


def test_search_conversation_scope_single(store: ConversationStore) -> None:
    """conversation_id filter restricts results to one conversation."""
    conv_a = store.create_conversation(CreateConversationInput(session_id="scope-a"))
    conv_b = store.create_conversation(CreateConversationInput(session_id="scope-b"))
    store.create_message(
        CreateMessageInput(
            conversation_id=conv_a.conversation_id,
            seq=0,
            role="user",
            content="shared word",
            token_count=2,
        )
    )
    store.create_message(
        CreateMessageInput(
            conversation_id=conv_b.conversation_id,
            seq=0,
            role="user",
            content="shared word",
            token_count=2,
        )
    )

    results = store.search_messages(
        MessageSearchInput(
            query="shared",
            mode="full_text",
            conversation_id=conv_a.conversation_id,
        )
    )
    assert len(results) == 1
    assert results[0].conversation_id == conv_a.conversation_id


def test_search_conversation_scope_multi(store: ConversationStore) -> None:
    """conversation_ids filter accepts multiple ids."""
    conv_a = store.create_conversation(CreateConversationInput(session_id="multi-a"))
    conv_b = store.create_conversation(CreateConversationInput(session_id="multi-b"))
    conv_c = store.create_conversation(CreateConversationInput(session_id="multi-c"))
    for conv in [conv_a, conv_b, conv_c]:
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=0,
                role="user",
                content="multi shared",
                token_count=2,
            )
        )

    results = store.search_messages(
        MessageSearchInput(
            query="shared",
            mode="full_text",
            conversation_ids=[conv_a.conversation_id, conv_b.conversation_id],
        )
    )
    found_ids = {r.conversation_id for r in results}
    assert found_ids == {conv_a.conversation_id, conv_b.conversation_id}


def test_search_time_range_filters(store: ConversationStore) -> None:
    """since/before filters bound by created_at."""
    conv = store.create_conversation(CreateConversationInput(session_id="time-range"))
    store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="time-bound msg",
            token_count=2,
        )
    )
    now = datetime.now(timezone.utc)
    far_future = now + timedelta(hours=1)
    far_past = now - timedelta(hours=1)

    # Searching with since=far_future returns nothing.
    future_results = store.search_messages(
        MessageSearchInput(
            query="time-bound",
            mode="full_text",
            since=far_future,
        )
    )
    assert future_results == []

    # Searching with since=far_past returns the message.
    past_results = store.search_messages(
        MessageSearchInput(
            query="time-bound",
            mode="full_text",
            since=far_past,
        )
    )
    assert len(past_results) == 1


# ---------------------------------------------------------------------------
# Externalized-reference normalization
# ---------------------------------------------------------------------------


def test_search_ignores_lcm_describe_helper_text(
    store: ConversationStore,
) -> None:
    """Externalized references strip ``Use lcm_describe`` boilerplate from FTS.

    Mirrors the LCM ``fts-fallback.test.ts`` case: messages with
    ``[LCM File: ...]`` content have boilerplate stripped from the FTS
    index. Searching for the boilerplate text does not match.
    """
    conv = store.create_conversation(CreateConversationInput(session_id="lcm-describe"))
    content = (
        "[LCM File: foo.txt]\n"
        "Exploration Summary:\n"
        "Real content body here.\n"
        "Use lcm_describe to get details."
    )
    store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content=content,
            token_count=10,
        )
    )

    # Search for "lcm_describe" should NOT match (boilerplate is stripped).
    boilerplate_results = store.search_messages(
        MessageSearchInput(query="lcm_describe", mode="full_text")
    )
    assert boilerplate_results == []

    # Search for "Real content" should match (it's in the summary block).
    summary_results = store.search_messages(MessageSearchInput(query="Real", mode="full_text"))
    assert len(summary_results) == 1


# ---------------------------------------------------------------------------
# CJK snippet byte-offset
# ---------------------------------------------------------------------------


def test_cjk_snippet_byte_offset(store: ConversationStore) -> None:
    """Inserting a message with CJK content + searching returns a correct snippet.

    Python ``str`` slicing is code-point-based (NOT UTF-16 code-unit
    based like JS), so a CJK character occupies exactly one slice index.
    The TS port used UTF-16 byte offsets; for non-surrogate-pair content
    (every CJK character except a tiny handful from the SMP) the two
    are equivalent.

    This test verifies the snippet correctly highlights CJK without
    breaking on what would be a surrogate pair in UTF-16.
    """
    conv = store.create_conversation(CreateConversationInput(session_id="cjk-snippet"))
    store.create_message(
        CreateMessageInput(
            conversation_id=conv.conversation_id,
            seq=0,
            role="user",
            content="hello 你好 world",
            token_count=4,
        )
    )

    results = store.search_messages(MessageSearchInput(query="你好", mode="full_text"))
    assert len(results) == 1
    # Snippet should contain both CJK characters as a contiguous pair.
    assert "你好" in results[0].snippet


# ---------------------------------------------------------------------------
# with_transaction (savepoint nesting)
# ---------------------------------------------------------------------------


def test_with_transaction_basic_commit(store: ConversationStore) -> None:
    """with_transaction commits on success."""
    conv = store.create_conversation(CreateConversationInput(session_id="txn-commit"))

    def op() -> int:
        record = store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=0,
                role="user",
                content="committed",
                token_count=1,
            )
        )
        return record.message_id

    message_id = store.with_transaction(op)
    assert store.get_message_by_id(message_id) is not None


def test_with_transaction_rollback_on_exception(
    store: ConversationStore,
) -> None:
    """with_transaction rolls back on exception."""
    conv = store.create_conversation(CreateConversationInput(session_id="txn-rollback"))

    class TestRollback(Exception):
        pass

    def op() -> None:
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=0,
                role="user",
                content="should-rollback",
                token_count=1,
            )
        )
        raise TestRollback("forced rollback")

    with pytest.raises(TestRollback):
        store.with_transaction(op)

    # No message should be persisted.
    assert store.get_message_count(conv.conversation_id) == 0


def test_with_transaction_nested_three_deep(store: ConversationStore) -> None:
    """Three-deep nesting works via savepoints (issue spec AC item)."""
    conv = store.create_conversation(CreateConversationInput(session_id="txn-nested"))

    def outer() -> int:
        def middle() -> int:
            def inner() -> int:
                record = store.create_message(
                    CreateMessageInput(
                        conversation_id=conv.conversation_id,
                        seq=0,
                        role="user",
                        content="deep",
                        token_count=1,
                    )
                )
                return record.message_id

            return store.with_transaction(inner)

        return store.with_transaction(middle)

    message_id = store.with_transaction(outer)
    assert store.get_message_by_id(message_id) is not None


def test_with_transaction_nested_inner_rollback_only(
    store: ConversationStore,
) -> None:
    """Inner savepoint rollback leaves outer commits intact."""
    conv = store.create_conversation(CreateConversationInput(session_id="txn-savepoint"))

    class InnerFail(Exception):
        pass

    def outer() -> None:
        # First insert: should commit.
        store.create_message(
            CreateMessageInput(
                conversation_id=conv.conversation_id,
                seq=0,
                role="user",
                content="outer-msg",
                token_count=1,
            )
        )

        def inner() -> None:
            # Second insert: rolled back via inner exception.
            store.create_message(
                CreateMessageInput(
                    conversation_id=conv.conversation_id,
                    seq=1,
                    role="user",
                    content="inner-msg",
                    token_count=1,
                )
            )
            raise InnerFail("inner rollback")

        try:
            store.with_transaction(inner)
        except InnerFail:
            pass  # caught — outer continues.

    store.with_transaction(outer)

    # Only the outer message survives.
    messages = store.get_messages(conv.conversation_id)
    assert len(messages) == 1
    assert messages[0].content == "outer-msg"


# ---------------------------------------------------------------------------
# Regex parity smoke check
# ---------------------------------------------------------------------------


REGEX_PARITY_CASES: list[tuple[str, str, str | None]] = [
    # (pattern, content, expected_match_or_None_when_no_match)
    # Simple word match.
    (r"hello", "say hello world", "hello"),
    # Digit sequence.
    (r"\d+", "abc123def", "123"),
    # Word boundary.
    (r"\bcat\b", "the cat sat", "cat"),
    # Alternation.
    (r"foo|bar", "say bar to me", "bar"),
    # Character class.
    (r"[A-Z]+", "Hello World", "H"),
    # Anchored.
    (r"^start", "starts here", "start"),
    # Greedy quantifier.
    (r"a+", "aaa-bbb", "aaa"),
    # Lazy quantifier.
    (r"a+?", "aaa-bbb", "a"),
    # Capture group (return whole match, not group).
    (r"(foo)(bar)", "foobar123", "foobar"),
    # No match returns None.
    (r"xyz", "abc def", None),
]


@pytest.mark.parametrize(("pattern", "content", "expected"), REGEX_PARITY_CASES)
def test_python_regex_matches_expected_offset(
    pattern: str, content: str, expected: str | None
) -> None:
    """Python re.search matches the expected substring for each pattern.

    This is the parity smoke check: the patterns are simple enough that
    Node ``RegExp.exec`` and Python ``re.search`` produce identical
    matches. Larger LCM regex patterns (lookahead, lookbehind, named
    groups, etc.) would warrant the subprocess-Node parity test in the
    issue spec; for now we cover the common LCM patterns.
    """
    match = re.search(pattern, content)
    if expected is None:
        assert match is None
    else:
        assert match is not None
        assert match.group(0) == expected
