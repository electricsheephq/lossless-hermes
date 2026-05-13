"""Byte-identical port of lossless-claw's ``buildMessageIdentityHash``.

This module is the smallest by LOC in the port (the function body is six
lines), but it is the most cross-runtime-load-bearing: the SHA-256 recipe
must produce **exactly** the digest that the Node implementation produces, so
that an existing OpenClaw ``~/.openclaw/lcm.db`` re-ingests into Python
``lossless-hermes`` without dedup drift (per spike-003 §"Test cases" — 10/10
byte-identical across Node/Python/Go).

Recipe (canonical):

    sha256(utf8(role) + b"\\x00" + utf8(content)).hexdigest()

The 0x00 separator is **not** a delimiter in the
"role and content cannot contain it" sense — spike-003 §"Case #6 details"
proves that an embedded NUL inside ``content`` (e.g. ``"before\\x00after"``)
hashes deterministically and that the typed ``MessageRole`` union upstream
forbids NUL-injection into ``role``. SHA-256 doesn't care about delimiter
ambiguity; the dedup invariant only requires the ``(role_bytes,
content_bytes)`` pair to round-trip identically.

See:

* ``/Volumes/LEXAR/Claude/lossless-claw/src/store/message-identity.ts`` (LCM
  commit ``1f07fbd``) — the canonical 13-LOC TS source.
* ``docs/spike-results/003-identity-hash.md`` — the 10-case parity fixture
  + worked example for ``("user", "hello")``.
* ``epics/01-storage/01-07-message-identity.md`` — this module's issue spec.
* ADR-003 — "OpenClaw migration is ``cp``" — the load-bearing decision that
  this byte-identity recipe enables.
"""

from __future__ import annotations

import hashlib


def build_message_identity_key(role: str, content: str) -> str:
    """In-memory dedup key. NOT persisted.

    Mirrors TS ``buildMessageIdentityKey(role, content)``: returns
    ``f"{role}\\x00{content}"`` so a ``dict``/``Map`` lookup can dedup within
    a single ingest pass. Use :func:`build_message_identity_hash` for any
    value that lands in the database.

    Args:
        role: The message role (typed upstream as the ``MessageRole`` union
            ``"user" | "assistant" | "system" | "tool" | …``; this function
            does not validate).
        content: The message content (already-canonicalized plain string;
            structured-content reduction is the caller's job — see
            ``extractMessageContent`` in LCM ``src/engine.ts:765-788``).

    Returns:
        ``f"{role}\\x00{content}"`` — a Python ``str`` containing a literal
        NUL codepoint. NOT a hex digest.
    """
    return f"{role}\x00{content}"


def build_message_identity_hash(role: str, content: str) -> str:
    """Byte-identical port of lossless-claw's ``buildMessageIdentityHash``.

    Recipe: ``sha256(utf8(role) + b"\\x00" + utf8(content)).hexdigest()``.

    Cross-checked against the Node implementation
    (``src/store/message-identity.ts``) and the Go TUI port
    (``tui/message_identity.go``) — all three produce identical digests on
    the 10-case fixture from spike-003 covering ASCII, CJK, ZWJ emoji
    families, embedded NUL bytes, JSON-stringified arrays, newlines/tabs,
    8 KiB content, and an empty-string boundary.

    Note: ``.encode("utf-8")`` is explicit. Per spike-003 §"Remaining 5%
    risk" row 2, omitting the argument would still resolve to UTF-8 on every
    CPython build the project supports (≥ 3.11) — but explicit beats
    implicit when the byte-identity invariant is load-bearing.

    Args:
        role: The message role string (typed upstream; see
            :func:`build_message_identity_key`).
        content: The canonical message content. Must be the **exact**
            string that LCM stored in ``messages.content`` — do NOT
            re-derive from ``message_parts`` (would produce a different
            hash if ``JSON.stringify`` field order differs across
            runtimes; see spike-003 §"Structured content normalization").

    Returns:
        Lowercase hex SHA-256 digest (64 chars).
    """
    h = hashlib.sha256()
    h.update(role.encode("utf-8"))
    h.update(b"\x00")
    h.update(content.encode("utf-8"))
    return h.hexdigest()
