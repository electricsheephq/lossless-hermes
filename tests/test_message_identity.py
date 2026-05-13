"""Byte-parity fixture for :mod:`lossless_hermes.store.message_identity`.

The 10 cases below are the spike-003 fixture verbatim — each ``(role, content,
expected_digest)`` triple is the hex digest produced by the canonical Node
implementation (``lossless-claw/src/store/message-identity.ts`` at LCM commit
``1f07fbd``). If any single case fails, the cross-runtime dedup invariant
breaks and an existing OpenClaw ``~/.openclaw/lcm.db`` will not re-ingest into
``lossless-hermes`` without proliferating duplicate rows.

This file is the load-bearing test for ADR-003 ("OpenClaw migration is
``cp``"). It MUST stay byte-pinned. A future refactor that adds NFC
normalization or swaps the separator will fail loudly here — which is the
intended behavior.

See:

* ``docs/spike-results/003-identity-hash.md`` §"Test cases" — the same
  10-row table.
* ``epics/01-storage/01-07-message-identity.md`` §"Acceptance criteria" — the
  same fixture, listed as AC item #2.
"""

from __future__ import annotations

import hashlib

import pytest

from lossless_hermes.store.message_identity import (
    build_message_identity_hash,
    build_message_identity_key,
)

# ---------------------------------------------------------------------------
# The 10-case spike-003 fixture — DO NOT EDIT without re-running the spike.
# Each row is (role, content, expected_hex_digest) where the digest is the
# value produced by the canonical Node implementation. See spike-003
# §"Test cases" for the same table.
# ---------------------------------------------------------------------------

SPIKE_003_FIXTURES: list[tuple[str, str, str]] = [
    # 1. Empty role + empty content — boundary case.
    ("", "", "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d"),
    # 2. ASCII baseline — the worked example in spike-003.
    ("user", "hello", "87ce4613405ac8c20165d125a5c2219e8b38a9e030616dffd73a89faaf7293c8"),
    # 3. CJK in role.
    ("ユーザー", "test", "9d886d80e62f390c46f3c016ab6c9414a336636e25b84ac4388b0766776a33b8"),
    # 4. CJK + fullwidth comma in content.
    ("user", "你好，世界", "c41afcf16ca44f0dba277cf25d3714fef56b15e112a9366c4f7a8c0d7eda71e7"),
    # 5. Emoji with skin-tone modifier + ZWJ family.
    (
        "assistant",
        "wave 👋🏽 and family 👨‍👩‍👧‍👦",
        "ddcb2103e8518fc5d3ff4b46cb73feb9d937c2089fd8904b6167e34ccbcc70f0",
    ),
    # 6. Embedded NUL inside content — the subtle case from spike-003
    # §"Case #6 details". Verifies separator-collision is NOT a bug.
    ("user", "before\x00after", "0926790e68cbb7d71293a854a1eea4da21a85baa07026a44dda869cdff489ce1"),
    # 7. JSON-stringified structured-content array (the exact form
    # ``extractMessageContent`` produces for non-text blocks).
    (
        "assistant",
        '[{"type":"text","text":"hi"}]',
        "d4eabe9e108ca7f2b6e88c44f70ce0263869a2f4e4901caa6499d959663609ee",
    ),
    # 8. Newlines + tabs.
    (
        "user",
        "line1\nline2\tcol",
        "a00dbd25b1c39636b6da4b8cf92c5968ec9b588202716ee9a4412644e943e620",
    ),
    # 9. 8 KiB content (8192 bytes).
    (
        "user",
        "abcdefgh" * 1024,
        "6ef15f41c013747b867624db7e116fc7d394cc90f538f62f38ffadd11811e17e",
    ),
    # 10. Tool result.
    ("tool", "result text", "60bd6dd0bf56004e2d0134016b977027273225b72f80d688ed04cc744f983faa"),
]


@pytest.mark.parametrize(
    ("role", "content", "expected_digest"),
    SPIKE_003_FIXTURES,
    ids=[
        "01-empty-role-and-content",
        "02-ascii-user-hello",
        "03-cjk-in-role",
        "04-cjk-fullwidth-in-content",
        "05-emoji-skintone-zwj-family",
        "06-embedded-nul-inside-content",
        "07-json-stringified-array",
        "08-newlines-and-tabs",
        "09-8kib-content",
        "10-tool-result",
    ],
)
def test_spike_003_byte_parity(role: str, content: str, expected_digest: str) -> None:
    """Each spike-003 case must produce the exact Node-side digest.

    A failure here means the byte-identity invariant has drifted from the
    Node implementation — STOP and investigate before merging. The most
    likely cause is an unintended Unicode normalization or a separator
    change in either runtime.
    """
    assert build_message_identity_hash(role, content) == expected_digest


# ---------------------------------------------------------------------------
# The single-case AC #1 spelled out (also covered by parametrize above, but
# kept as a standalone test so a failure shows the most-likely-to-be-quoted
# case in stack traces — AC item #1 of issue 01-07).
# ---------------------------------------------------------------------------


def test_ac1_user_hello_digest() -> None:
    """AC #1: the ``("user", "hello")`` case must produce the published digest.

    This is the worked example in spike-003 §"Worked example (case #2)" and
    is also called out as the first acceptance criterion in the issue spec.
    """
    assert (
        build_message_identity_hash("user", "hello")
        == "87ce4613405ac8c20165d125a5c2219e8b38a9e030616dffd73a89faaf7293c8"
    )


# ---------------------------------------------------------------------------
# Edge / property checks beyond the spike-003 fixture.
# ---------------------------------------------------------------------------


def test_returns_lowercase_hex_64_chars() -> None:
    """The output is always a 64-char lowercase hex string (SHA-256 hex)."""
    digest = build_message_identity_hash("user", "anything")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert digest == digest.lower()


def test_role_change_changes_digest() -> None:
    """Changing the role flips the digest — basic sanity that role is hashed."""
    a = build_message_identity_hash("user", "hello")
    b = build_message_identity_hash("assistant", "hello")
    assert a != b


def test_content_change_changes_digest() -> None:
    """Changing the content flips the digest — sanity that content is hashed."""
    a = build_message_identity_hash("user", "hello")
    b = build_message_identity_hash("user", "world")
    assert a != b


def test_deterministic_across_calls() -> None:
    """Same inputs always produce the same digest (no global state, no salt)."""
    inputs = ("user", "deterministic check")
    first = build_message_identity_hash(*inputs)
    second = build_message_identity_hash(*inputs)
    third = build_message_identity_hash(*inputs)
    assert first == second == third


def test_matches_manual_sha256_recipe() -> None:
    """The function is byte-equal to the explicit recipe.

    This is a second cross-check at the bytes level: if a refactor ever
    accidentally swaps the implementation for an HMAC, a different hash, or
    a different separator, this test fails independently of the pinned
    fixture.
    """
    role, content = "user", "hello"
    expected = hashlib.sha256(role.encode("utf-8") + b"\x00" + content.encode("utf-8")).hexdigest()
    assert build_message_identity_hash(role, content) == expected


# ---------------------------------------------------------------------------
# build_message_identity_key — the in-memory sister function (AC #5).
# ---------------------------------------------------------------------------


def test_key_returns_role_nul_content() -> None:
    """AC #5: ``build_message_identity_key`` returns ``f"{role}\\x00{content}"``.

    Used for in-memory dedup (Map/dict keys) — NOT persisted, NOT hashed.
    """
    assert build_message_identity_key("user", "hello") == "user\x00hello"


def test_key_includes_separator_for_empty_inputs() -> None:
    """The NUL separator is always present, even when role and content are
    both empty — so ``("", "")`` does not collide with ``("a", "b")`` after
    truncation of a single character; the key still differentiates inputs.
    """
    assert build_message_identity_key("", "") == "\x00"
    assert build_message_identity_key("a", "b") == "a\x00b"
    assert build_message_identity_key("", "") != build_message_identity_key("a", "b")


def test_key_preserves_embedded_nul_in_content() -> None:
    """Like the hash, the key preserves the embedded NUL inside content.

    A naive split-on-NUL parser would mis-recover the original pair — which
    is why the key is documented as in-memory-only and never persisted.
    """
    assert build_message_identity_key("user", "before\x00after") == "user\x00before\x00after"


# ---------------------------------------------------------------------------
# Negative-space edge cases not in the 10-row spike fixture but useful for
# regression coverage: lone surrogate handling, very long content, and the
# "role is bytes-equal to content" symmetry case.
# ---------------------------------------------------------------------------


def test_empty_role_nonempty_content_hashes() -> None:
    """An empty role still hashes; the result is just ``sha256(b"\\x00" + content)``."""
    digest = build_message_identity_hash("", "non-empty")
    manual = hashlib.sha256(b"\x00non-empty").hexdigest()
    assert digest == manual


def test_nonempty_role_empty_content_hashes() -> None:
    """A non-empty role with empty content also hashes deterministically."""
    digest = build_message_identity_hash("user", "")
    manual = hashlib.sha256(b"user\x00").hexdigest()
    assert digest == manual


def test_role_and_content_separator_symmetry() -> None:
    """``("ab", "c")`` and ``("a", "bc")`` MUST produce different digests.

    Without a separator they would be byte-equal as the concatenation
    ``"abc"`` — this test confirms the separator is doing its job. Per
    spike-003 §"Case #6 details" this is the core argument for the 0x00
    delimiter even when content can contain a NUL: collisions of this form
    require role to contain a NUL too, which is forbidden upstream by the
    ``MessageRole`` typed union.
    """
    assert build_message_identity_hash("ab", "c") != build_message_identity_hash("a", "bc")
