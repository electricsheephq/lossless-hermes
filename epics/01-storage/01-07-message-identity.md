---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port store/message-identity.ts → store/message_identity.py'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/store/message-identity.ts`
- Lines: ~13 LOC (the file is tiny; the test fixture is the load-bearing part).
- Function(s)/class(es): `buildMessageIdentityKey(role, content) -> string`, `buildMessageIdentityHash(role, content) -> string`.

## Target (Python)

- File: `src/lossless_hermes/store/message_identity.py`
- Estimated LOC: ~20

## What this issue covers

**The smallest module by LOC, but the most cross-runtime-load-bearing.** A byte-identical port of the TS recipe so that an existing OpenClaw `~/.openclaw/lcm.db` re-ingests into Python `lossless-hermes` without dedup drift (per spike-003).

### The recipe (per spike-003)

```python
import hashlib


def build_message_identity_key(role: str, content: str) -> str:
    """In-memory dedup key. NOT persisted."""
    return f"{role}\x00{content}"


def build_message_identity_hash(role: str, content: str) -> str:
    """Byte-identical port of lossless-claw's buildMessageIdentityHash.

    Recipe: sha256(utf8(role) + b'\\x00' + utf8(content)).hex()

    Cross-checked against the Node implementation
    (src/store/message-identity.ts) and the Go TUI port
    (tui/message_identity.go) — all three produce identical digests
    on a 10-case fixture (spike-003).
    """
    h = hashlib.sha256()
    h.update(role.encode("utf-8"))
    h.update(b"\x00")
    h.update(content.encode("utf-8"))
    return h.hexdigest()
```

### Spike-003 byte-parity fixture (port verbatim)

10 (role, content, expected_digest) triples covering:

1. Empty role + empty content.
2. ASCII (`"user"`, `"hello"`).
3. CJK in role (`"ユーザー"`, `"test"`).
4. CJK + fullwidth comma in content (`"user"`, `"你好，世界"`).
5. Emoji with skin-tone + ZWJ family (`"assistant"`, `"wave 👋🏽 and family 👨‍👩‍👧‍👦"`).
6. Embedded NUL inside content (`"user"`, `"before\x00after"`) — the subtle case; verifies separator-collision is NOT a bug.
7. JSON-stringified array (`"assistant"`, `'[{"type":"text","text":"hi"}]'`).
8. Newlines + tabs (`"user"`, `"line1\nline2\tcol"`).
9. 8 KiB content (`"user"`, `"abcdefgh" * 1024`).
10. Tool result (`"tool"`, `"result text"`).

Each triple's expected hex digest is the value produced by the canonical Node implementation. Per spike-003 §"Test cases": the diff between `node-results.json` and `python-results.json` is empty across all 10 cases.

### What this enables

- **OpenClaw migration is `cp`** (per ADR-003 + storage.md §10.1). The existing `messages.identity_hash` column survives the file copy; the Python `hasMessage()` / `countMessagesByIdentity()` queries return identical answers.
- **Backfill of NULL identity_hash columns** (storage.md §12 risk #5, spike-003 §"Remaining 5% risk" row 3) — re-running `build_message_identity_hash` over `(role, content)` for rows with `identity_hash IS NULL OR identity_hash = ''` produces the same value the Node code would have, so no drift.

### Out of scope

- The `hasMessage` / `countMessagesByIdentity` SQL queries — those live in ConversationStore (#01-08).
- Content-extraction logic (`extractMessageContent` at `src/engine.ts:765-788`) — that's Epic 02. **Per spike-003 §"Structured content normalization":** the Python re-ingester reads `messages.content` verbatim from the existing column; it does NOT re-derive content from `message_parts`.

## Dependencies

- Depends on: #00-01 (scaffolding only — this module is pure-function and uses only stdlib).
- Blocks: #01-08 (ConversationStore — auto-computes `identity_hash` on `createMessage`), #01-15 (`identity_hash` backfill for legacy rows).

## Acceptance criteria

- [ ] `build_message_identity_hash("user", "hello")` returns `"87ce4613405ac8c20165d125a5c2219e8b38a9e030616dffd73a89faaf7293c8"` (spike-003 case #2).
- [ ] All **10 spike-003 fixture cases** pass byte-identical:
  - `("", "", "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d")`
  - `("user", "hello", "87ce4613405ac8c20165d125a5c2219e8b38a9e030616dffd73a89faaf7293c8")`
  - `("ユーザー", "test", "9d886d80e62f390c46f3c016ab6c9414a336636e25b84ac4388b0766776a33b8")`
  - `("user", "你好，世界", "c41afcf16ca44f0dba277cf25d3714fef56b15e112a9366c4f7a8c0d7eda71e7")`
  - `("assistant", "wave 👋🏽 and family 👨‍👩‍👧‍👦", "ddcb2103e8518fc5d3ff4b46cb73feb9d937c2089fd8904b6167e34ccbcc70f0")`
  - `("user", "before\x00after", "0926790e68cbb7d71293a854a1eea4da21a85baa07026a44dda869cdff489ce1")`
  - `("assistant", '[{"type":"text","text":"hi"}]', "d4eabe9e108ca7f2b6e88c44f70ce0263869a2f4e4901caa6499d959663609ee")`
  - `("user", "line1\nline2\tcol", "a00dbd25b1c39636b6da4b8cf92c5968ec9b588202716ee9a4412644e943e620")`
  - `("user", "abcdefgh" * 1024, "6ef15f41c013747b867624db7e116fc7d394cc90f538f62f38ffadd11811e17e")`
  - `("tool", "result text", "60bd6dd0bf56004e2d0134016b977027273225b72f80d688ed04cc744f983faa")`
- [ ] The 1 case from `test/message-identity.test.ts` (storage.md §8 row 16: "Exact-match lookup on identity_hash with many same-hash rows" — exercises the FK + index path) is ported to `tests/test_message_identity.py` (this case actually exercises the ConversationStore query path; coordinate with #01-08).
- [ ] Cross-runtime parity script under `scripts/verify_identity_hash_parity.py` runs all 10 cases through the Node implementation (via subprocess if Node is installed) and through Python; asserts byte-equal. Optional in CI (skipped if Node not present); recommended for the PR description.
- [ ] `build_message_identity_key` returns `f"{role}\x00{content}"` (not hashed — in-memory dedup only).
- [ ] No new mypy errors.
- [ ] PR description cites LCM commit `1f07fbd`, `src/store/message-identity.ts`, and spike-003.

## Estimated effort

**1 hour** — the function is trivial; the test fixture port is the work.

## Confidence

**98%** — spike-003 closes the parity question across Node, Python, and Go on a 10-case fixture. The only residual risk is a future TS refactor that adds NFC normalization or swaps the separator — the pinned fixture test catches that loudly.
