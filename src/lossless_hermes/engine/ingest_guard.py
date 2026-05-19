"""Ingest-time storage-boundary guard for oversized base64/media payloads.

**Issue #131 (v0.2.0).** `lossless-claw`'s ``ingestSingle`` runs interception
passes before the DB write (``engine.ts:5950-6022`` —
``interceptInlineImages`` / ``interceptLargeFiles`` /
``interceptLargeRawPayload``). The Python ``_ingest_single``
(``engine/ingest.py``) went straight to the DB transaction with zero
interception — the deferral is explicit in that module's
``_extract_message_content`` / ``_build_message_parts`` docstrings ("the
image interception pipeline at 5950-6022 [is] deferred to a v0.2
follow-up"). A ``data:image/...;base64,<huge>`` message therefore landed
raw in ``messages.content`` + the ``messages_fts`` shadow + the WAL + every
``db_backup`` copy.

This module closes that gap. It scans a message's ``content`` /
``tool_calls`` for inline ``data:`` base64 URIs and very long stand-alone
base64 runs, and — when found — externalizes the offending substring
through the **existing** :class:`~lossless_hermes.large_files.LargeFileManager`
(``large_files.py``), replacing it inline with a compact
:func:`~lossless_hermes.large_files.format_raw_payload_reference`
placeholder before the message reaches :meth:`_ingest_single`'s DB
transaction.

### Relationship to hermes-lcm

The production-tested reference is ``hermes-lcm/ingest_protection.py``
(``protect_messages_for_ingest`` / ``protect_message_for_ingest``,
lines 419-496). hermes-lcm has **no LICENSE**, so this is a clean
reimplementation from understanding rather than a verbatim copy:

* The **detection heuristics** — the ``data:`` URI regex, the long-run
  regex, and the conservative :func:`looks_like_long_base64` ratio gate —
  are reimplemented here because they are purpose-built for exactly the
  Hermes message shape and the conservatism is load-bearing (a too-eager
  gate would externalize JWTs, hashes, and ordinary prose).
* The **externalization target differs by design.** hermes-lcm routes
  payloads to a parallel JSON-file store (``externalize.py``); per issue
  #131 this port routes them through the existing ``LargeFileManager``
  disk + ``large_files``-table sidecar instead of building a second
  externalization store. The placeholder text is therefore
  :func:`format_raw_payload_reference`'s ``[LCM Raw Payload: ...]`` block,
  not hermes-lcm's ``[Externalized LCM ingest payload: ...]`` string.

### Why a substring guard and not a whole-message threshold

A 4 MB screenshot data URI can ride inside an otherwise small message
(a one-line ``"here is the screenshot: data:image/png;base64,..."``).
A whole-message token threshold (``large_file_token_threshold``) would
miss it whenever the surrounding text keeps the message under budget.
The guard scans *substrings* so the binary-ish payload is externalized
regardless of how large the carrier message is.

### Idempotency

Re-ingesting an already-protected message is a no-op: the
:func:`format_raw_payload_reference` placeholder is plain text with no
``data:``-URI and no long base64 run, so a second pass finds nothing to
externalize. The diff-cursor in ``ingest.py`` is the primary dedup
mechanism; this property is the belt-and-suspenders guarantee for the
``post_llm_call`` + ``handle_tool_call`` double-fire path (ADR-009).

### Non-blocking contract

Externalization is best-effort. If :class:`LargeFileManager` cannot write
the blob (disk full, FK failure) the guard logs a warning and leaves the
inline content **untouched** — losslessness is never sacrificed to the
guard. A raw payload in SQLite is a storage-hygiene regression; a dropped
payload is data loss. The guard always prefers the former.

See:

* ``hermes-lcm/ingest_protection.py:419-496`` — the reference shape.
* ``lossless-claw/src/engine.ts:5950-6022`` — the TS interception passes.
* ``src/lossless_hermes/large_files.py`` — :class:`LargeFileManager` +
  :func:`format_raw_payload_reference`.
* ``docs/adr/002-plugin-data-directory.md`` — large-file directory layout.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from lossless_hermes.large_files import (
    LargeFileManager,
    format_raw_payload_reference,
)

logger = logging.getLogger("lossless_hermes.engine.ingest_guard")

__all__ = [
    "contains_data_uri_base64",
    "contains_long_base64_run",
    "looks_like_long_base64",
    "protect_message_for_ingest",
    "protect_messages_for_ingest",
]


# ---------------------------------------------------------------------------
# Detection thresholds
# ---------------------------------------------------------------------------

# Minimum length of a stand-alone base64 run before it is even considered
# for externalization. Reimplemented from hermes-lcm's
# ``_GENERIC_BASE64_MIN_CHARS`` (4096). 4096 base64 chars decode to ~3 KB of
# binary — comfortably above any hash/ID/JWT-ish snippet, so the gate stays
# conservative. Anything shorter is left inline.
_GENERIC_BASE64_MIN_CHARS = 4096

# Minimum payload length inside a ``data:...;base64,`` URI before it is
# externalized. 256 base64 chars (~192 decoded bytes) is short enough to
# catch tiny inline icons but long enough to never fire on a ``data:`` URI
# that carries a trivial inline SVG-as-text or a few bytes.
_DATA_URI_MIN_PAYLOAD_CHARS = 256


# ---------------------------------------------------------------------------
# Detection regexes
# ---------------------------------------------------------------------------

# A raw scan can see JSON-escaped slashes before any JSON decode happens —
# both the literal ``\/`` escape and the unicode ``/`` escape can
# appear inside a tool-call ``arguments`` string that was serialized by an
# upstream provider. Treat all three slash spellings as a slash so the
# regex still binds when the payload arrives JSON-escaped. Reimplemented
# from hermes-lcm's ``_JSON_ESCAPED_SLASH_RE``.
_JSON_ESCAPED_SLASH = r"(?:/|\\/|\\u002[fF])"

# Any ``data:<mediatype>;base64,<payload>`` URI — not just image/audio/video.
# The trailing payload alphabet is deliberately conservative (base64 chars
# plus the escaped-slash spellings) so the match stops at the first
# surrounding JSON/markdown delimiter instead of slurping the rest of the
# string. The ``{N,}`` floor keeps trivially-short data URIs inline.
# Reimplemented from hermes-lcm's ``_DATA_URI_BASE64_RE``.
_DATA_URI_BASE64_RE = re.compile(
    rf"data:(?:[A-Za-z0-9.+-]|{_JSON_ESCAPED_SLASH})*"
    rf"(?:;[A-Za-z0-9_.+%-]+=(?:[-A-Za-z0-9_.+%]|{_JSON_ESCAPED_SLASH})*)*"
    rf";base64,(?:[A-Za-z0-9+=]|{_JSON_ESCAPED_SLASH}){{{_DATA_URI_MIN_PAYLOAD_CHARS},}}"
    rf"(?=$|[^A-Za-z0-9+/=])",
    re.IGNORECASE,
)

# A stand-alone run of base64-alphabet characters, bounded by non-alphabet
# characters on both sides so we capture exactly one clean run. The capture
# group is then re-checked by :func:`looks_like_long_base64` — the regex is
# a cheap pre-filter, the function is the real gate. Reimplemented from
# hermes-lcm's ``_BASE64_RUN_RE``.
_BASE64_RUN_RE = re.compile(
    rf"(?<![A-Za-z0-9+/=_-])([A-Za-z0-9+/=_-]{{{_GENERIC_BASE64_MIN_CHARS},}})(?![A-Za-z0-9+/=_-])"
)

# A string composed only of base64-alphabet characters plus whitespace.
# Used as a fast reject inside :func:`looks_like_long_base64`.
_BASE64_ALPHABET_RE = re.compile(r"^[A-Za-z0-9+/=_\s-]+$")


def contains_data_uri_base64(text: str) -> bool:
    """Return ``True`` if ``text`` holds a ``data:...;base64,`` URI of size.

    Only fires for payloads at or above :data:`_DATA_URI_MIN_PAYLOAD_CHARS`
    base64 characters — a tiny inline data URI is left alone.
    """
    return isinstance(text, str) and bool(_DATA_URI_BASE64_RE.search(text))


def looks_like_long_base64(text: str, *, min_chars: int = _GENERIC_BASE64_MIN_CHARS) -> bool:
    """Conservative "is this a long binary-ish base64 payload" heuristic.

    Reimplemented from hermes-lcm's ``looks_like_long_base64``. The
    conservatism is the whole point: a too-eager gate would externalize
    JWTs, git hashes, UUIDs, and ordinary prose. To match, a string must

    * be at least ``min_chars`` long, both raw and after whitespace is
      stripped — short tokens never match;
    * NOT have a length ``% 4 == 1`` — no valid base64 has that residue,
      so such a run is almost certainly not base64;
    * contain only base64-alphabet characters plus whitespace;
    * be at least 98% base64-alphabet characters by ratio — a run with a
      handful of stray punctuation is treated as prose, not a payload;
    * use at least 8 distinct characters (ignoring trailing ``=`` pad) —
      a 5000-character run of one repeated character is a degenerate log
      line, not a binary payload.

    PEM blocks, ordinary logs, and source code contain delimiters,
    headers, or whitespace that keep them from matching as one clean run.

    Args:
        text: The candidate substring (typically a :data:`_BASE64_RUN_RE`
            capture group).
        min_chars: Minimum run length. Defaults to
            :data:`_GENERIC_BASE64_MIN_CHARS`.

    Returns:
        ``True`` only when every conservative condition holds.
    """
    if not isinstance(text, str) or len(text) < min_chars:
        return False
    compact = "".join(text.split())
    if len(compact) < min_chars:
        return False
    # No valid base64 string has a length residue of 1 mod 4.
    if len(compact) % 4 == 1:
        return False
    if not _BASE64_ALPHABET_RE.match(text):
        return False
    base64_chars = sum(1 for ch in text if ch.isalnum() or ch in "+/=_-")
    ratio = base64_chars / max(1, len(text))
    if ratio < 0.98:
        return False
    # Require a bit of alphabet mixing so a long run of one repeated
    # character (a degenerate log line) is not treated as binary.
    return len(set(compact.rstrip("="))) >= 8


def contains_long_base64_run(text: str, *, min_chars: int = _GENERIC_BASE64_MIN_CHARS) -> bool:
    """Return ``True`` if ``text`` holds at least one long base64 run.

    A run is "long" when it clears both :data:`_BASE64_RUN_RE` (the cheap
    regex pre-filter) and :func:`looks_like_long_base64` (the conservative
    gate). Reimplemented from hermes-lcm's ``contains_long_base64_run``.
    """
    if not isinstance(text, str) or len(text) < min_chars:
        return False
    return any(
        looks_like_long_base64(match.group(1), min_chars=min_chars)
        for match in _BASE64_RUN_RE.finditer(text)
    )


# ---------------------------------------------------------------------------
# Externalization
# ---------------------------------------------------------------------------


def _externalize_payload(
    payload: str,
    *,
    manager: LargeFileManager,
    conversation_id: int,
    role: str,
    reason: str,
) -> str | None:
    """Externalize one payload substring through :class:`LargeFileManager`.

    Writes the payload to the large-file disk + ``large_files``-table
    sidecar and returns the compact
    :func:`format_raw_payload_reference` placeholder to substitute inline.

    Non-blocking: any failure (disk full, FK violation, integrity error)
    is caught, logged at WARNING, and returns ``None`` so the caller keeps
    the inline content. A raw payload in SQLite is a hygiene regression;
    a dropped payload is data loss — the guard always prefers the former.

    Args:
        payload: The exact substring to externalize (a ``data:`` URI or a
            long base64 run).
        manager: The wired :class:`LargeFileManager`.
        conversation_id: The owning conversation's PK (FK on
            ``large_files``).
        role: The message role, surfaced in the placeholder.
        reason: A short externalization reason, surfaced in the
            placeholder (e.g. ``"data_uri_base64"`` or
            ``"inline_base64_run"``).

    Returns:
        The placeholder string on success; ``None`` on any failure.
    """
    if not payload:
        return None
    # ``image/...`` data URIs are media; record a media-ish mime hint so
    # the large_files row is self-describing. The exact mime is best-effort.
    mime_type: str | None = None
    lowered = payload[:64].lower()
    if lowered.startswith("data:"):
        # ``data:image/png;base64,...`` → ``image/png``.
        head = payload.split(";", 1)[0]
        candidate = head[len("data:") :].strip()
        if candidate:
            mime_type = candidate
    try:
        record = manager.externalize_block(
            conversation_id=conversation_id,
            content=payload,
            mime_type=mime_type,
        )
    except Exception as exc:  # noqa: BLE001 — non-blocking guard contract
        logger.warning(
            "[lcm] ingest guard: could not externalize a %d-char %s payload "
            "for conversation_id=%s (%s); preserving inline content for "
            "lossless recovery",
            len(payload),
            reason,
            conversation_id,
            exc,
        )
        return None
    return format_raw_payload_reference(
        file_id=record.file_id,
        role=role,
        byte_size=record.byte_size if record.byte_size is not None else len(payload),
        reason=reason,
        summary=(
            f"Externalized at ingest: {reason}. "
            f"{record.byte_size or len(payload)} bytes moved out of SQLite "
            f"to keep the messages table, FTS shadow, WAL, and backups compact."
        ),
    )


def _protect_text(
    text: str,
    *,
    manager: LargeFileManager,
    conversation_id: int,
    role: str,
) -> str:
    """Externalize every oversized base64/media substring inside ``text``.

    Runs two passes: first the ``data:`` base64 URI pass, then the
    generic long-base64-run pass on the result. Each match is replaced
    inline with its placeholder; a failed externalization leaves that
    match's text untouched.

    Returns ``text`` unchanged when nothing matches.
    """
    if not text:
        return text

    def replace_data_uri(match: re.Match[str]) -> str:
        payload = match.group(0)
        return (
            _externalize_payload(
                payload,
                manager=manager,
                conversation_id=conversation_id,
                role=role,
                reason="data_uri_base64",
            )
            or payload
        )

    protected = _DATA_URI_BASE64_RE.sub(replace_data_uri, text)

    def replace_base64_run(match: re.Match[str]) -> str:
        payload = match.group(1)
        # The regex is a cheap pre-filter; the conservative gate is the
        # real decision. A run that clears the regex but fails the gate
        # (a JWT, a hash, a one-character log line) stays inline.
        if not looks_like_long_base64(payload):
            return payload
        return (
            _externalize_payload(
                payload,
                manager=manager,
                conversation_id=conversation_id,
                role=role,
                reason="inline_base64_run",
            )
            or payload
        )

    return _BASE64_RUN_RE.sub(replace_base64_run, protected)


def _protect_value(
    value: Any,
    *,
    manager: LargeFileManager,
    conversation_id: int,
    role: str,
) -> Any:
    """Recursively externalize base64/media payloads in an arbitrary value.

    Walks dicts and lists so a payload nested inside an Anthropic content
    block (``{"type": "image", "source": {"data": "<base64>"}}``) or an
    OpenAI tool-call ``arguments`` structure is still reached. Strings are
    handed to :func:`_protect_text`; scalars (int/float/bool/None) are
    returned as-is.

    A fresh container is returned only when something actually changed
    inside it — an untouched subtree is returned by identity so callers
    can cheaply detect "nothing happened".
    """
    if isinstance(value, dict):
        changed = False
        out: Dict[Any, Any] = {}
        for key, val in value.items():
            protected = _protect_value(
                val,
                manager=manager,
                conversation_id=conversation_id,
                role=role,
            )
            if protected is not val:
                changed = True
            out[key] = protected
        return out if changed else value
    if isinstance(value, list):
        changed = False
        items: List[Any] = []
        for item in value:
            protected = _protect_value(
                item,
                manager=manager,
                conversation_id=conversation_id,
                role=role,
            )
            if protected is not item:
                changed = True
            items.append(protected)
        return items if changed else value
    if not isinstance(value, str):
        return value
    protected_text = _protect_text(
        value,
        manager=manager,
        conversation_id=conversation_id,
        role=role,
    )
    return protected_text if protected_text != value else value


def protect_message_for_ingest(
    message: Dict[str, Any],
    *,
    manager: LargeFileManager,
    conversation_id: int,
) -> Dict[str, Any]:
    """Return a copy of ``message`` safe to persist in SQLite.

    Scans ``message["content"]`` and ``message["tool_calls"]`` for inline
    ``data:`` base64 URIs and long stand-alone base64 runs. Each oversized
    payload is externalized through :class:`LargeFileManager` and replaced
    inline with a compact :func:`format_raw_payload_reference` placeholder.

    The input ``message`` is **not** mutated — a shallow copy is taken and
    only the touched keys are rewritten. When nothing matches, the copy is
    structurally identical to the input (the guard is a no-op in the
    common case).

    Args:
        message: The raw message dict from ``conversation_history``.
        manager: The wired :class:`LargeFileManager` (disk + DB sidecar).
        conversation_id: The owning conversation's PK — required for the
            ``large_files`` FK.

    Returns:
        A copy of ``message`` with oversized payloads externalized. Safe
        to feed straight into :meth:`_ingest_single`'s persistence path.
    """
    if not isinstance(message, dict):
        return message

    msg = dict(message)
    role = str(msg.get("role") or "unknown")

    if "content" in msg and msg["content"] is not None:
        msg["content"] = _protect_value(
            msg["content"],
            manager=manager,
            conversation_id=conversation_id,
            role=role,
        )

    if msg.get("tool_calls"):
        msg["tool_calls"] = _protect_value(
            msg["tool_calls"],
            manager=manager,
            conversation_id=conversation_id,
            role=role,
        )

    return msg


def protect_messages_for_ingest(
    messages: List[Dict[str, Any]],
    *,
    manager: LargeFileManager,
    conversation_id: int,
) -> List[Dict[str, Any]]:
    """Apply :func:`protect_message_for_ingest` to every message in a batch.

    A thin convenience wrapper. The per-message guard already takes a
    copy, so this is a pure map with no shared mutable state.
    """
    return [
        protect_message_for_ingest(
            message,
            manager=manager,
            conversation_id=conversation_id,
        )
        for message in messages
    ]
