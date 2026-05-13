"""Ingest methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the ``_on_post_llm_call`` hook handler + ``_ingest_single`` /
``_ingest_batch`` helpers per ADR-027 §Decision "Package structure" —
the ``ingest.py`` sub-module of ``src/lossless_hermes/engine/``.

**Issue 03-02** fills in the bodies of ``_on_post_llm_call``,
``_ingest_single``, and ``_ingest_batch`` per ADR-009 §Decision
"Option B + safety net Option C": diff the snapshot
``conversation_history`` Hermes hands the hook against
``self._last_seen_message_idx[session_id]``, ingest each new message
into the LCM SQLite DAG, advance the index.

**Sync, not async:** PR #34 (merged 2026-05-13) converted the hook
callback surface from ``async def`` → ``def``. Hermes's
``PluginManager.invoke_hook`` (``hermes_cli/plugins.py:1218-1232``)
calls callbacks via ``ret = cb(**kwargs)`` with no ``await`` /
``asyncio.run`` — so an ``async def`` callback would return a coroutine
that Hermes would treat as a non-``None`` result and append to its
``results`` list. Per-session in-process serialization runs through
:meth:`SessionLockRegistry.acquire_sync` (a sibling sync surface added
at 03-02 alongside the existing async :meth:`SessionLockRegistry.acquire`
from PR #26 / issue 02-08). Cross-process serialization continues to
ride on SQLite WAL + ``lcm_worker_lock`` per ADR-018 §Decision.

**Coverage note (ADR-009 §Consequences):** ``post_llm_call`` is gated
on ``final_response and not interrupted`` at ``run_agent.py:15407`` —
turns that exit on Ctrl-C or no-final-response mid-tool-loop never
fire this hook. The next successful turn will diff from
``_last_seen_message_idx[session_id]`` and pick up the
previous-turn-interrupted tail on its way through. Issue 03-03 adds
the belt-and-suspenders safety net by hooking ``handle_tool_call`` /
``on_session_end`` for the residual coverage gap.

Mixin contract (per ADR-027 §Consequences "All state lives on the shell
class"):

* No state owned here. Methods read/write
  ``self._last_seen_message_idx``, ``self._conversation_store``,
  ``self._summary_store``, ``self._session_locks`` exclusively via the
  shell class's attributes declared in :meth:`LCMEngine.__init__`.
* No cross-mixin imports. If ingest work needs assemble behavior, it
  goes through ``self.assemble(...)`` (MRO resolves to
  :class:`_AssembleMixin`).

See:

* ``docs/adr/009-per-message-ingest.md`` — ``post_llm_call`` as the
  per-turn ingest seam (the hook this body handles).
* ``docs/adr/018-concurrency-model.md`` — per-session lock invariant.
* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/adr/029-wave-fix-provenance.md`` — Wave-N provenance comments.
* ``docs/porting-guides/engine.md`` §"ingest" — the TS algorithm that
  this file ports.
* ``lossless-claw/src/engine.ts`` lines 5899-6134 — TS ``ingestSingle``
  / ``ingest`` / ``ingestBatch`` source.
* ``epics/03-ingest-assembly/03-02-ingest-diff-on-turn.md`` — this
  issue's spec (with the sync-override caveat).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from lossless_hermes.store.conversation import (
        ConversationStore,
        CreateMessagePartInput,
    )
    from lossless_hermes.store.summary import SummaryStore

    from .session_locks import SessionLockRegistry


logger = logging.getLogger("lossless_hermes.engine.ingest")


# ---------------------------------------------------------------------------
# Module-private helpers (port of engine.ts:765-1149)
# ---------------------------------------------------------------------------

# Persistable message roles per TS ``hasPersistableMessageRole``
# (engine.ts:1097-1106). The Python role normalization happens via
# :func:`_to_db_role`; here we just gate on the upstream raw role string
# being one of these. ``toolResult`` is a Codex-shape alias for
# ``tool``; both pass the gate.
_PERSISTABLE_RAW_ROLES = frozenset({"user", "assistant", "system", "tool", "toolResult"})

# Tool-raw block types per TS ``TOOL_RAW_TYPES`` (engine.ts:508-520).
# Used by :func:`_extract_message_content` to recognize tool-only
# content arrays (which storage represents as empty content + the
# structured detail lives in ``message_parts``).
_TOOL_RAW_TYPES: frozenset[str] = frozenset({
    "tool_use",
    "toolUse",
    "tool-use",
    "toolCall",
    "tool_call",
    "functionCall",
    "function_call",
    "function_call_output",
    "tool_result",
    "toolResult",
    "tool_use_result",
})


def _to_db_role(role: Any) -> str:
    """Normalize an upstream role string to the DB role enum.

    Ports TS ``toDbRole`` (engine.ts:1079-1095): collapse ``toolResult``
    → ``tool``; passthrough ``user`` / ``assistant`` / ``system``;
    fallback to ``assistant`` for unknown shapes (matches TS behavior —
    the upstream filter at :func:`_has_persistable_role` already
    rejected anything not in :data:`_PERSISTABLE_RAW_ROLES`, so this
    fallback only fires on programmer error).

    Args:
        role: The raw role from the upstream message dict. ``Any``
            because Hermes may pass a non-string in degenerate cases.

    Returns:
        One of ``"user"`` / ``"assistant"`` / ``"system"`` / ``"tool"``.
    """
    if role == "tool" or role == "toolResult":
        return "tool"
    if role == "system":
        return "system"
    if role == "user":
        return "user"
    if role == "assistant":
        return "assistant"
    return "assistant"


def _has_persistable_role(message: Dict[str, Any]) -> bool:
    """Return True if ``message["role"]`` is a persistable role.

    Ports TS ``hasPersistableMessageRole`` (engine.ts:1097-1106).
    """
    return message.get("role") in _PERSISTABLE_RAW_ROLES


def _extract_message_content(content: Any) -> str:
    """Reduce structured content to the plain-text fallback string.

    Ports the **simplified** v0.1 form of TS ``extractMessageContent``
    (engine.ts:765-788) — for the issue 03-02 v0.1 port we keep the
    text-only externalization path (spec §"Required state"): handle
    ``None`` / empty / string / list cases verbatim; for structured
    blocks (Anthropic content blocks, OpenAI tool_calls, etc.) we fall
    back to ``json.dumps`` of the whole shape. The richer recursive
    ``extractStructuredText`` walk (engine.ts:540-647) is a v0.2
    deferral — it covers nested ``text`` / ``output`` / ``result``
    field extraction across 6 levels of depth, with JSON-payload
    detection. For v0.1, ``json.dumps(content)`` is the conservative
    fallback that preserves all structure in ``messages.content`` —
    the structured detail still lands in ``message_parts`` via
    :func:`_build_message_parts` regardless of how
    ``messages.content`` is shaped, so no information is lost.

    TODO (issue 03-XX follow-up): port the full recursive
    ``extractStructuredText`` walk (engine.ts:540-647) once the v0.1
    ingest path is exercised end-to-end. The deferral is documented
    in spec line 72 ("port the simplest of these (text-only
    externalization); large-file/binary externalization can ship as a
    follow-up if blocking").

    Args:
        content: The ``message["content"]`` value (any shape).

    Returns:
        The plain-text fallback string for ``messages.content``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if len(content) == 0:
            return ""
        # If every element is a tool-only block per TS engine.ts:778-784,
        # store as empty (the structured data lives in message_parts).
        all_tool_raw = all(
            isinstance(item, dict)
            and isinstance(item.get("type"), str)
            and item.get("type") in _TOOL_RAW_TYPES
            for item in content
        )
        if all_tool_raw:
            return ""
    # Fall back to JSON-serialized form so downstream FTS index still
    # has *some* searchable text. ``default=str`` covers non-JSON-
    # serializable shapes (datetimes, sets, etc.) without raising.
    try:
        return json.dumps(content, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        # Defensive — should never fire given ``default=str`` above.
        return str(content)


def _safe_string(value: Any) -> Optional[str]:
    """Return ``value`` if it's a non-empty string, else None.

    Mirrors TS ``safeString`` (used throughout engine.ts for the
    metadata extraction logic in :func:`_build_message_parts`). Treats
    the empty string as "missing" — matches TS truthiness check.
    """
    if isinstance(value, str) and value:
        return value
    return None


def _safe_bool(value: Any) -> Optional[bool]:
    """Return ``value`` if it's a bool, else None.

    Mirrors TS ``safeBoolean`` for metadata extraction.
    """
    if isinstance(value, bool):
        return value
    return None


def _to_json_metadata(record: Dict[str, Any]) -> Optional[str]:
    """Serialize a metadata dict to JSON, stripping ``None`` values.

    Ports TS ``toJson`` (used by :func:`_build_message_parts` for the
    metadata column). Returns ``None`` if the result would be empty
    (no metadata to record).
    """
    # Drop None entries so the stored JSON stays compact and the FTS
    # index doesn't pick up boilerplate ``"key": null`` keys.
    cleaned = {k: v for k, v in record.items() if v is not None}
    if not cleaned:
        return None
    try:
        return json.dumps(cleaned, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _estimate_tokens(text: str) -> int:
    """Estimate token count for ``text``.

    Prefers the canonical :func:`lossless_hermes.estimate_tokens
    .estimate_tokens` port (issue 03-01 / PR #35). When that module is
    not yet installed (issue 03-02 lands before #35 merges to main on
    some build), falls back to the naive ``ceil(len(text) / 4)``
    heuristic. The fallback is intentionally crude — it's only the
    transient state between PR #34 (which 03-02 depends on, merged)
    and PR #35 (which 03-02 does NOT block, may merge after).

    The proper port (PR #35) handles CJK + emoji weighting per
    ADR-021; the fallback underestimates non-ASCII corpora by 4-6×.
    Once PR #35 lands on main, this fallback path goes dead — the
    canonical import becomes the only path.
    """
    try:
        # PR #35 (port/03-01-token-estimator) lands the canonical port.
        # Until it merges to main this import resolves on the dev tree
        # (PR #35's branch checkout) and falls through on main.
        from lossless_hermes.estimate_tokens import (  # ty: ignore[unresolved-import]
            estimate_tokens,
        )

        return estimate_tokens(text)
    except ImportError:
        # PR #35 not yet on main. Fallback to TS naive shape just so
        # ingest can compute *some* token_count. The order-of-magnitude
        # estimate is good enough for the compaction trigger gate at
        # this point (Epic 04's real compaction algorithm reads
        # token_count, and Epic 04 strictly depends on PR #35 for its
        # own compaction-decision math).
        if not text:
            return 0
        return (len(text) + 3) // 4


def _build_message_parts(
    session_id: str,
    message: Dict[str, Any],
    fallback_content: str,
) -> List["CreateMessagePartInput"]:
    """Build ``message_parts`` rows for a single message.

    Ports the v0.1-essential subset of TS ``buildMessageParts``
    (engine.ts:903-1093). The full TS implementation walks structured
    content arrays (Anthropic blocks, OpenAI tool_calls), normalizes
    ``rawType`` → ``part_type`` via ``toPartType``, and pulls
    ``tool_call_id`` / ``tool_name`` / ``tool_input`` / ``tool_output``
    out of multiple field-name aliases. The v0.1 port covers:

    * String-content single-text-part case (engine.ts:965-983) —
      stores one ``text`` part with the message content.
    * List-content multi-block case (engine.ts:1002-1077) — emits one
      part per element, classifying each as ``text`` (default) or
      ``tool`` (when the block ``type`` is in :data:`_TOOL_RAW_TYPES`).
      Tool-block ``tool_call_id`` / ``tool_name`` / ``tool_input`` /
      ``tool_output`` columns are populated from the documented field
      aliases.
    * Non-string, non-list content (engine.ts:986-1000) — falls back to
      a single ``agent`` part carrying the fallback content.

    The richer image-block / native-image-block / bash-execution-shape
    paths (engine.ts:933-963 + the image interception pipeline at
    5950-6022) are deferred to a v0.2 follow-up (spec line 72
    "v0.1.0, port the simplest of these (text-only externalization)").

    Args:
        session_id: The owning session id (FK column on every part).
        message: The raw message dict from ``conversation_history``.
        fallback_content: The plain-text fallback computed by
            :func:`_extract_message_content` — used as the
            ``text_content`` for parts that have no inline text and as
            the body of the fallback ``agent`` part for unknown shapes.

    Returns:
        Ordered list of :class:`CreateMessagePartInput` ready for
        :meth:`ConversationStore.create_message_parts`.
    """
    # Deferred import to avoid the module-init circular (engine init →
    # ingest module → store → engine).
    from lossless_hermes.store.conversation import CreateMessagePartInput

    role = message.get("role", "unknown")
    role_str = role if isinstance(role, str) else "unknown"

    top_level_tool_call_id = (
        _safe_string(message.get("toolCallId"))
        or _safe_string(message.get("tool_call_id"))
        or _safe_string(message.get("toolUseId"))
        or _safe_string(message.get("tool_use_id"))
        or _safe_string(message.get("call_id"))
        or _safe_string(message.get("id"))
    )
    top_level_tool_name = _safe_string(message.get("toolName")) or _safe_string(
        message.get("tool_name")
    )
    top_level_is_error = _safe_bool(message.get("isError")) or _safe_bool(message.get("is_error"))

    if "content" not in message:
        # TS engine.ts:949-963 — unknown-shape fallback.
        return [
            CreateMessagePartInput(
                session_id=session_id,
                part_type="agent",
                ordinal=0,
                text_content=fallback_content or None,
                metadata=_to_json_metadata({
                    "originalRole": role_str,
                    "source": "unknown-message-shape",
                }),
            )
        ]

    content = message["content"]
    if isinstance(content, str):
        # TS engine.ts:965-983 — single text part.
        return [
            CreateMessagePartInput(
                session_id=session_id,
                part_type="text",
                ordinal=0,
                text_content=content,
                tool_call_id=top_level_tool_call_id,
                tool_name=top_level_tool_name,
                metadata=_to_json_metadata({
                    "originalRole": role_str,
                    "isError": top_level_is_error,
                }),
            )
        ]

    if not isinstance(content, list):
        # TS engine.ts:986-1000 — non-array, non-string content shape.
        return [
            CreateMessagePartInput(
                session_id=session_id,
                part_type="agent",
                ordinal=0,
                text_content=fallback_content or None,
                metadata=_to_json_metadata({
                    "originalRole": role_str,
                    "source": "non-array-content",
                }),
            )
        ]

    # TS engine.ts:1002-1077 — multi-block content array.
    parts: List["CreateMessagePartInput"] = []
    for ordinal, block in enumerate(content):
        if not isinstance(block, dict):
            # Defensive — non-dict elements stored as raw-text part.
            parts.append(
                CreateMessagePartInput(
                    session_id=session_id,
                    part_type="text",
                    ordinal=ordinal,
                    text_content=str(block) if block is not None else None,
                    metadata=_to_json_metadata({
                        "originalRole": role_str,
                        "source": "non-dict-block",
                    }),
                )
            )
            continue

        block_type = _safe_string(block.get("type"))
        is_tool_block = block_type in _TOOL_RAW_TYPES if block_type else False
        part_type: str = "tool" if is_tool_block else "text"

        text_content: Optional[str] = None
        if isinstance(block.get("text"), str):
            text_content = block["text"]
        elif isinstance(block.get("content"), str):
            text_content = block["content"]

        # Tool block field aliases (engine.ts:1015-1048).
        tool_call_id = (
            _safe_string(block.get("toolCallId"))
            or _safe_string(block.get("tool_call_id"))
            or _safe_string(block.get("toolUseId"))
            or _safe_string(block.get("tool_use_id"))
            or _safe_string(block.get("call_id"))
            or (_safe_string(block.get("id")) if is_tool_block else None)
            or top_level_tool_call_id
        )
        tool_name = (
            _safe_string(block.get("name"))
            or _safe_string(block.get("toolName"))
            or _safe_string(block.get("tool_name"))
            or top_level_tool_name
        )

        # Serialize tool_input / tool_output via the documented aliases.
        # ``json.dumps`` preserves arbitrary shapes; ``default=str``
        # tolerates non-JSON-native values.
        tool_input: Optional[str] = None
        if "input" in block:
            tool_input = json.dumps(block["input"], default=str, ensure_ascii=False)
        elif "arguments" in block:
            tool_input = json.dumps(block["arguments"], default=str, ensure_ascii=False)
        elif "toolInput" in block:
            tool_input = json.dumps(block["toolInput"], default=str, ensure_ascii=False)
        elif isinstance(block.get("tool_input"), str):
            tool_input = block["tool_input"]

        tool_output: Optional[str] = None
        if "output" in block:
            tool_output = json.dumps(block["output"], default=str, ensure_ascii=False)
        elif "toolOutput" in block:
            tool_output = json.dumps(block["toolOutput"], default=str, ensure_ascii=False)
        elif isinstance(block.get("tool_output"), str):
            tool_output = block["tool_output"]

        parts.append(
            CreateMessagePartInput(
                session_id=session_id,
                part_type=part_type,
                ordinal=ordinal,
                text_content=text_content,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=tool_output,
                metadata=_to_json_metadata({
                    "originalRole": role_str,
                    "blockType": block_type,
                    "isError": top_level_is_error,
                }),
            )
        )

    return parts


def _is_failed_empty_assistant(message: Dict[str, Any]) -> bool:
    """Return True for assistant messages with ``stopReason=error|aborted`` + empty content.

    Ports TS engine.ts:5919-5938. These occur on transient API failures
    (500s) — ingesting them pollutes the LCM DAG because retry adds
    each error message to the running prompt, creating a positive
    feedback loop where each retry is bigger and more malformed.
    """
    if message.get("role") != "assistant":
        return False
    stop_reason = message.get("stopReason") or message.get("stop_reason")
    if stop_reason not in ("error", "aborted"):
        return False
    content = message.get("content")
    if content is None or content == "":
        return True
    if isinstance(content, list) and len(content) == 0:
        return True
    return False


def _matches_pattern_list(candidate: str, patterns: List[re.Pattern[str]]) -> bool:
    """Return True if ``candidate`` matches any compiled regex pattern.

    Ports TS ``matchesSessionPattern`` (session-patterns.ts:21-23).
    The compiled-pattern source is :attr:`LCMEngine.ignore_session_patterns`
    / :attr:`LCMEngine.stateless_session_patterns`.
    """
    return any(p.search(candidate) for p in patterns)


# ---------------------------------------------------------------------------
# Mixin class
# ---------------------------------------------------------------------------


class _IngestMixin:
    """Ingest hook handlers for :class:`LCMEngine`.

    Maps to engine.ts ``ingestSingle`` (lines 5899-6064), ``ingest``
    (6066-6090), ``ingestBatch`` (6092-6134), and the ``post_llm_call``
    hook handler per ADR-009.

    Issue 03-02 fills in the bodies of ``_on_post_llm_call`` /
    ``_ingest_single`` / ``_ingest_batch``. The hook surface is
    **synchronous** (PR #34) and acquires the per-session sync lock via
    :meth:`SessionLockRegistry.acquire_sync` before mutating DB state.
    """

    # ------------------------------------------------------------------
    # Type-checker stubs (TYPE_CHECKING-only) declaring the shell state
    # this mixin reads/writes. Per ADR-027 §Consequences "All state lives
    # on the shell class", the real attribute creation happens in
    # :meth:`LCMEngine.__init__`.
    # ------------------------------------------------------------------
    if TYPE_CHECKING:
        _db: Optional[sqlite3.Connection]
        _conversation_store: Optional[ConversationStore]
        _summary_store: Optional[SummaryStore]
        _session_locks: SessionLockRegistry
        _last_seen_message_idx: Dict[str, int]
        ignore_session_patterns: List[re.Pattern[str]]
        stateless_session_patterns: List[re.Pattern[str]]
        config: Any  # LcmConfig — avoid the circular import for ty

    # ------------------------------------------------------------------
    # Gate helpers — match TS engine.ts:1932-1959
    # ------------------------------------------------------------------

    def _should_ignore_session(
        self,
        *,
        session_id: Optional[str],
        session_key: Optional[str] = None,
    ) -> bool:
        """Return True when ``session_id`` matches ``ignore_session_patterns``.

        Ports TS ``shouldIgnoreSession`` (engine.ts:1932-1946). When no
        patterns are configured this is a fast-path False — the empty
        list early-return matters because ``re.search`` on each pattern
        in a hot path would otherwise impose a per-turn cost for the
        common (zero-pattern) configuration.

        Args:
            session_id: The Hermes session identifier.
            session_key: Optional cross-conversation identity (preferred
                when present, per TS:1937-1940). Hermes plugin hook
                kwargs do not surface ``session_key`` today; the param
                is accepted for forward-compat.

        Returns:
            ``True`` iff at least one pattern matches the (preferred-
            sessionKey-then-sessionId) candidate string.
        """
        patterns = self.ignore_session_patterns
        if not patterns:
            return False
        candidate = (
            session_key.strip()
            if isinstance(session_key, str) and session_key.strip()
            else (session_id or "").strip()
        )
        if not candidate:
            return False
        return _matches_pattern_list(candidate, patterns)

    def _is_stateless_session(self, session_key: Optional[str]) -> bool:
        """Return True when ``session_key`` matches ``stateless_session_patterns``.

        Ports TS ``isStatelessSession`` (engine.ts:1949-1959). Gates
        on ``config.skip_stateless_sessions`` (default True) AND a
        non-empty trimmed ``session_key``. Hermes hook kwargs do not
        surface ``session_key`` today; this gate is forward-compat —
        it returns False whenever ``session_key`` is missing.
        """
        if not getattr(self.config, "skip_stateless_sessions", True):
            return False
        trimmed = session_key.strip() if isinstance(session_key, str) else ""
        if not trimmed:
            return False
        patterns = self.stateless_session_patterns
        if not patterns:
            return False
        return _matches_pattern_list(trimmed, patterns)

    # ------------------------------------------------------------------
    # Public hook handler
    # ------------------------------------------------------------------

    def _on_post_llm_call(
        self,
        session_id: str = "",
        user_message: Any = None,
        assistant_response: str = "",
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        model: str = "",
        platform: str = "",
        **kwargs: Any,
    ) -> None:
        """``post_llm_call`` Hermes hook — diff new messages + ingest.

        Replaces engine.ts ``afterTurn()`` (lines 6220-6646) ingest
        portion per ADR-009 (post_llm_call as the per-turn ingest seam).
        Diffs ``conversation_history[self._last_seen_message_idx[session_id]:]``
        and ingests each new entry via :meth:`_ingest_batch`. After a
        successful ingest of N>0 new messages, advances
        ``self._last_seen_message_idx[session_id]`` to
        ``len(conversation_history)``.

        **Observer-only contract.** Per
        ``docs/reference/hermes-hooks.md`` line 92, ``post_llm_call``
        return values are ignored — and exceptions inside the hook
        would crash the Hermes turn loop. The body catches every
        exception, logs it, and returns ``None`` so a transient DB
        error or a malformed message can never break the user-facing
        agent.

        Args:
            session_id: The Hermes session identifier.
            user_message: The user's latest turn content. Unused at
                03-02 — diff is over ``conversation_history``.
            assistant_response: The assistant's latest response.
                Unused at 03-02 — same reason as ``user_message``.
            conversation_history: Full message history snapshot. The
                diff source. ``None`` is tolerated for forward-compat
                / partial-kwarg callers and treated as empty.
            model: The LLM model id. Unused at 03-02.
            platform: The provider platform string. Unused at 03-02.
            **kwargs: Forward-compat for future hook additions.
        """
        try:
            self._do_post_llm_call(
                session_id=session_id,
                conversation_history=conversation_history,
            )
        except Exception as exc:  # noqa: BLE001 — observer-only contract
            # ADR-009 §Consequences + hermes-hooks.md line 92 — the hook
            # MUST NOT raise. A transient DB error, malformed message,
            # or any other failure logs and returns None so the agent
            # loop continues. The error surface for operators is the
            # log; downstream telemetry (Epic 08 doctor) reads logs.
            logger.error(
                "[lcm] post_llm_call ingest failed for session=%s: %s",
                session_id,
                exc,
                exc_info=True,
            )

    def _do_post_llm_call(
        self,
        *,
        session_id: str,
        conversation_history: Optional[List[Dict[str, Any]]],
    ) -> None:
        """Inner body of :meth:`_on_post_llm_call` — raises on error.

        Split out so the public hook's try/except can stay narrow:
        every code path in this body either short-circuits or runs
        through the per-session lock. Tests that need to assert specific
        exception types call this method directly.
        """
        # Fast-fail guards before any lock acquisition.
        if not session_id:
            logger.debug("[lcm] post_llm_call: empty session_id, skipping ingest")
            return

        # Engine not yet bootstrapped (``on_session_start`` hasn't run
        # for this process). The hook fires on the FIRST user turn AFTER
        # ``on_session_start``, so this branch should be unreachable in
        # production — but Hermes can fire callbacks during teardown
        # races, so we degrade gracefully.
        if self._conversation_store is None or self._summary_store is None:
            logger.warning(
                "[lcm] post_llm_call session=%s: stores not initialized "
                "(on_session_start did not run?); skipping ingest",
                session_id,
            )
            return

        # Session-filter gates (ports TS engine.ts:6072-6077 +
        # 6098-6103). Stateless gate runs first since it's the more
        # narrowly-scoped (depends on a non-empty session_key); ignore
        # gate is the broader bypass.
        if self._should_ignore_session(session_id=session_id):
            logger.debug(
                "[lcm] post_llm_call session=%s: ignored by pattern",
                session_id,
            )
            return
        if self._is_stateless_session(None):
            # Hook kwargs don't carry session_key today; the gate
            # short-circuits unless/until Hermes forwards it.
            logger.debug(
                "[lcm] post_llm_call session=%s: stateless session, skipping writes",
                session_id,
            )
            return

        history = conversation_history or []
        last_idx = self._last_seen_message_idx.get(session_id, 0)
        if last_idx >= len(history):
            # No new messages — idempotent no-op. Matches the spec AC
            # "Re-running the hook with the same ``conversation_history``
            # is a no-op".
            logger.debug(
                "[lcm] post_llm_call session=%s: no new messages (last_idx=%d, history_len=%d)",
                session_id,
                last_idx,
                len(history),
            )
            return

        new_messages = history[last_idx:]
        # Lock-and-ingest. The per-session sync lock guards the
        # diff → ingest → cursor-advance sequence so concurrent firings
        # on the same session_id (in the gateway, two adjacent turns
        # from the same conversation can race when one runs long) see
        # FIFO serialization. Cross-session ingests parallelize.
        with self._session_locks.acquire_sync(session_id):
            # Re-read last_idx under the lock to handle the case where
            # a concurrent firing advanced it while we were queued.
            # Without this re-read we'd re-ingest already-persisted
            # messages, which the identity_hash UNIQUE constraint would
            # reject — but the cleaner path is to recompute the diff
            # window from the latest cursor before doing any writes.
            current_idx = self._last_seen_message_idx.get(session_id, 0)
            if current_idx >= len(history):
                logger.debug(
                    "[lcm] post_llm_call session=%s: another caller "
                    "advanced the cursor while we waited; nothing to do",
                    session_id,
                )
                return
            window = history[current_idx:]
            ingested = self._ingest_batch(session_id=session_id, messages=window)
            if ingested > 0:
                self._last_seen_message_idx[session_id] = len(history)
                logger.info(
                    "[lcm] post_llm_call session=%s: ingested %d/%d new messages (cursor %d -> %d)",
                    session_id,
                    ingested,
                    len(window),
                    current_idx,
                    len(history),
                )
            else:
                logger.debug(
                    "[lcm] post_llm_call session=%s: 0 of %d candidate "
                    "messages ingested (all filtered/dropped)",
                    session_id,
                    len(window),
                )

    # ------------------------------------------------------------------
    # _ingest_batch / _ingest_single — TS engine.ts:5899-6134 port
    # ------------------------------------------------------------------

    def _ingest_batch(
        self,
        *,
        session_id: str,
        messages: List[Dict[str, Any]],
        session_key: Optional[str] = None,
    ) -> int:
        """Ingest a batch of messages; return the count actually persisted.

        Ports TS ``ingestBatch`` (engine.ts:6092-6134). Each message
        runs through :meth:`_ingest_single` under the caller's lock
        (NOT under a fresh lock acquisition — the public entry point
        :meth:`_on_post_llm_call` already holds the per-session lock,
        and re-entering would deadlock on the non-reentrant
        :class:`threading.Lock`).

        Empty input is a no-op (returns 0). Ignored / stateless
        sessions short-circuit at the caller side; the batch path
        itself does NOT re-check those gates per spec line 92
        ("``ingestBatch`` just loops ``ingestSingle`` under one queue
        acquisition").

        Args:
            session_id: The session identifier.
            messages: New messages to ingest. May be empty.
            session_key: Optional cross-conversation identity. Not
                surfaced by Hermes hooks today; forward-compat param.

        Returns:
            Count of messages actually persisted (after role-gate,
            failed-empty-assistant gate, and any per-message early
            return from :meth:`_ingest_single`).
        """
        if not messages:
            return 0
        count = 0
        for message in messages:
            try:
                if self._ingest_single(
                    session_id=session_id,
                    message=message,
                    session_key=session_key,
                ):
                    count += 1
            except Exception as exc:  # noqa: BLE001
                # Per-message error isolation: one bad message in a
                # batch must not abort the others. The Wave-4 atomic
                # transaction inside :meth:`_ingest_single` already
                # rolls back the bad message's partial writes; here we
                # log + continue so the rest of the batch lands. The
                # outer :meth:`_on_post_llm_call` handler will see a
                # smaller ingested count and decide whether to advance
                # the cursor.
                logger.error(
                    "[lcm] _ingest_batch: single ingest failed for session=%s message_role=%s: %s",
                    session_id,
                    message.get("role") if isinstance(message, dict) else "?",
                    exc,
                    exc_info=True,
                )
        return count

    def _ingest_single(
        self,
        *,
        session_id: str,
        message: Dict[str, Any],
        session_key: Optional[str] = None,
    ) -> bool:
        """Ingest one message; return True iff a row was persisted.

        Ports TS ``ingestSingle`` (engine.ts:5899-6064). The five-step
        skip ladder mirrors TS:5906-5938:

        1. Heartbeat → skip (this body has no heartbeat kwarg yet —
           the Hermes hook surface does not surface ``is_heartbeat``,
           but the gate is preserved as a forward-compat seam).
        2. Non-persistable role → skip.
        3. Failed-empty-assistant → skip.

        After the skip ladder, runs the three-write atomic transaction:

        4. ``getMaxSeq`` → ``createMessage`` → ``createMessageParts``
           → ``appendContextMessage`` — all inside one
           ``BEGIN IMMEDIATE`` (see Wave-4 comment below). On any
           failure the txn rolls back, leaving no orphan rows. The
           caller treats a ``False`` return as "nothing happened".

        Args:
            session_id: The session identifier.
            message: The raw message dict.
            session_key: Optional cross-conversation identity (forward-
                compat; not used at v0.1).

        Returns:
            ``True`` iff a row was persisted, ``False`` if the message
            was skipped by any gate.
        """
        # Step 1+2: persistable role gate (TS:5909-5911).
        if not isinstance(message, dict):
            return False
        if not _has_persistable_role(message):
            return False

        # Step 3: failed-empty-assistant gate (TS:5919-5938 — Wave-N
        # adjacent regression guard for retry pollution loops).
        if _is_failed_empty_assistant(message):
            logger.debug(
                "[lcm] _ingest_single session=%s: skipping failed-empty assistant (stopReason=%s)",
                session_id,
                message.get("stopReason") or message.get("stop_reason"),
            )
            return False

        # Compute the storage triple: db_role / fallback content /
        # token_count. Done OUTSIDE the transaction so a malformed
        # message that explodes during content extraction does not
        # leave the DB partially written.
        db_role = _to_db_role(message.get("role"))
        fallback_content = _extract_message_content(message.get("content"))
        token_count = _estimate_tokens(fallback_content)

        # Get-or-create the conversation row. NOT wrapped in
        # :meth:`with_transaction` because the store's own create path
        # has UNIQUE-race recovery (TS engine.ts:5943-5946 — the row
        # may already exist when two adjacent turns of the same session
        # bootstrap concurrently). The conversation create lands its
        # OWN row before we begin the per-message txn.
        store = self._conversation_store
        if store is None:
            # Defense in depth — :meth:`_do_post_llm_call` already
            # guards this. If we reach here something else opened
            # a window post-guard.
            raise RuntimeError(
                "_ingest_single: conversation_store is None (on_session_start did not run?)"
            )
        summary_store = self._summary_store
        if summary_store is None:
            raise RuntimeError(
                "_ingest_single: summary_store is None (on_session_start did not run?)"
            )
        conversation = store.get_or_create_conversation(session_id, session_key=session_key)
        conversation_id = conversation.conversation_id

        parts = _build_message_parts(
            session_id=session_id,
            message=message,
            fallback_content=fallback_content,
        )

        # LCM Wave-4 (2026-01-XX): wrap the three-write ingest path in
        # a single SQLite transaction. Previously these ran as separate
        # ops:
        #   1. getMaxSeq + createMessage
        #   2. createMessageParts
        #   3. appendContextMessage
        # Failure modes if any one threw mid-sequence:
        #   - createMessageParts throws after createMessage → orphan
        #     message row with no parts → assembler emits malformed turn.
        #   - appendContextMessage throws after the first two → message
        #     persisted but invisible to assembler → permanent context gap.
        #   - Concurrent ingest race: two callers both read seq=N, both
        #     INSERT seq=N+1 → UNIQUE conflict, second caller's exception
        #     bubbles up after partial writes were already committed.
        # BEGIN IMMEDIATE (the body of
        # :meth:`ConversationStore.with_transaction`) locks SQLite for
        # write so seq computation + message INSERT happen atomically;
        # any throw rolls back the whole sequence.
        # Original: lossless-claw/src/engine.ts:6024-6063.
        def _persist() -> bool:
            assert store is not None  # for the type-checker; guarded above
            assert summary_store is not None
            max_seq = store.get_max_seq(conversation_id)
            seq = max_seq + 1
            try:
                from lossless_hermes.store.conversation import (
                    CreateMessageInput,
                )

                msg_record = store.create_message(
                    CreateMessageInput(
                        conversation_id=conversation_id,
                        seq=seq,
                        role=db_role,  # type: ignore[arg-type]
                        content=fallback_content,
                        token_count=token_count,
                    )
                )
            except sqlite3.IntegrityError as exc:
                # Concurrent-ingest race: another caller advanced
                # ``seq`` between our ``get_max_seq`` and ``INSERT``.
                # With BEGIN IMMEDIATE this should be unreachable
                # (the txn is exclusive for writes), but if a future
                # path opens a non-exclusive write transaction or the
                # ``identity_hash`` UNIQUE invariant from ADR-009
                # §"Identity hash invariant" is later added, treat the
                # collision as "already ingested by a concurrent racer"
                # — return False so the caller's batched count is
                # honest and re-raise nothing.
                msg_lower = str(exc).lower()
                if "unique constraint failed" in msg_lower:
                    logger.debug(
                        "[lcm] _ingest_single session=%s: UNIQUE race "
                        "on seq=%d (concurrent ingest); skipping. exc=%s",
                        session_id,
                        seq,
                        exc,
                    )
                    return False
                raise

            store.create_message_parts(msg_record.message_id, parts)
            summary_store.append_context_message(conversation_id, msg_record.message_id)
            return True

        return store.with_transaction(_persist)
