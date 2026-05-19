"""Assembly methods for :class:`~lossless_hermes.engine.LCMEngine`.

Hosts the per-turn assembly substitution + ``safe_fallback`` per
ADR-027 §Decision "Package structure" — the ``assemble.py`` sub-module
of ``src/lossless_hermes/engine/``.

### Issue 03-09 scope (closes Epic 03)

Wires the always-on assembly substitution path per ADR-010. Two paths
coexist:

* **Production (Option B)** — :meth:`preassemble` overrides the Hermes
  ``ContextEngine.preassemble`` ABC method (upstream PR #24949). Called
  every turn by ``run_agent.py`` BEFORE ``pre_llm_call`` with the live
  ``messages`` list and a budget. Returns the substituted list (or the
  original on any fallback). This is the path the production v1.0 ships
  on.

* **Experimental (Option A)** — when ``preassemble`` is ABSENT and
  ``experimental_always_on_via_compress`` is True, the engine routes
  substitution via :meth:`_CompactMixin.compress`. The wiring lives in
  :class:`_CompactMixin` (which overrides ``should_compress`` to return
  True every turn and ``compress`` to delegate to ``self._assemble``);
  this module owns the assembly body itself.

The shared assembly body — :meth:`_AssembleMixin._assemble` — handles
the engine-level policy that lives above
:meth:`ContextAssembler.assemble`:

1. Ignored-session bypass → :meth:`_safe_fallback`.
2. No-conversation lookup → :meth:`_safe_fallback`.
3. Cache-aware orphan-stripping-ordinal pinning (Epic 04 stub: cold).
4. Empty context-items short-circuit.
5. Raw-only-context-items-trailing-live short-circuit.
6. Delegate to :class:`ContextAssembler.assemble` (the 03-08 surface).
7. Empty-result or no-user-turn sanity checks.
8. Snapshot for next-turn prefix-stability diagnostics.
9. Return assembled messages.

The ``_assemble`` wrapper is **synchronous** (post-PR #34 hook
conversion). It calls :meth:`ContextAssembler.assemble` (sync — all
assembler stages are sync by design per Epic 03 spec). The
:meth:`SessionLockRegistry.acquire_sync` lock guards the DB-read +
assemble window per ADR-018.

### Issue 03-10 scope (already landed via PR #39)

The :meth:`_on_pre_llm_call` hook returns
``{"context": LOSSLESS_RECALL_POLICY_PROMPT}`` to Hermes's plugin
plumbing, which appends the policy text to the current turn's user-
message content per ADR-014 (preserves Anthropic prompt cache).

### Sync, not async

Per PR #34 (merged 2026-05-13), Hermes's hook callbacks are sync. Every
public method here is ``def``, not ``async def``. The substitution body
calls :meth:`ContextAssembler.assemble` synchronously and acquires
per-session locks via :meth:`SessionLockRegistry.acquire_sync`.

### Wave-N markers

Per ADR-029, the TS source at ``engine.ts:6648-6832`` carries NO
explicit Wave-N markers — the always-on assemble path survived all 12
audit waves without scar-tissue patches. Per the same ADR, no
``# LCM Wave-N`` provenance comments are required in this body.

Mixin contract (per ADR-027 §Consequences "All state lives on the shell
class"):

* No state owned here. Methods read/write
  ``self._previous_assembled_messages_by_conversation``,
  ``self._stable_orphan_stripping_ordinals_by_conversation``,
  ``self._summary_store``, etc. exclusively via the shell class's
  attributes declared in :meth:`LCMEngine.__init__`.
* No cross-mixin imports. If assemble work needs ingest behavior, it
  goes through ``self.ingest_batch(...)`` (MRO resolves to
  :class:`_IngestMixin`).

See:

* ``docs/adr/010-always-on-assembly.md`` — Option A vs Option B
  substitution mechanism.
* ``docs/adr/014-recall-policy-injection.md`` — user-message-position
  injection of the policy text (preserves Anthropic prompt cache).
* ``docs/adr/024-project-layout.md`` — engine/ package placement.
* ``docs/adr/027-engine-splitting.md`` — mixin pattern decisions.
* ``docs/spike-results/002-hermes-pre-llm-call.md`` — why
  ``pre_llm_call`` is append-only and cannot rewrite the message list.
* ``docs/upstream/001-preassemble-abc.md`` — upstream PR #24949 status.
* ``docs/porting-guides/engine.md`` §"assemble" + §"Always-on assembly
  problem" — TS algorithm + Python adaptation.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from lossless_hermes.recall_policy import LOSSLESS_RECALL_POLICY_PROMPT

if TYPE_CHECKING:
    # Type-only imports for the mixin's reads/writes against shell state.
    from lossless_hermes.assembler import AssembleResult
    from lossless_hermes.db.config import LcmConfig
    from lossless_hermes.engine.session_locks import SessionLockRegistry
    from lossless_hermes.store.conversation import ConversationStore
    from lossless_hermes.store.summary import SummaryStore


logger = logging.getLogger("lossless_hermes.engine.assemble")


# ---------------------------------------------------------------------------
# 03-09 constants
# ---------------------------------------------------------------------------

# Experimental-mode rate-limited warning cooldown. Per ADR-010 §"Path 2"
# and the issue spec AC line 8, the warning fires at engine init AND on
# every turn (rate-limited to once per minute). 60s gives the operator
# regular reminders without flooding logs on long-running gateways.
_EXPERIMENTAL_WARN_COOLDOWN_S: float = 60.0

# Default Hermes budget when the host doesn't pass one. The TS source
# (``engine.ts:6693``) uses 128_000 as the default cap. Matches the
# Claude 3.5 / 4 context window — a safe upper bound that any modern
# model accepts.
_DEFAULT_TOKEN_BUDGET: int = 128_000


# ---------------------------------------------------------------------------
# _AssembleMixin
# ---------------------------------------------------------------------------


class _AssembleMixin:
    """Per-turn assembly + ``safe_fallback`` handlers for :class:`LCMEngine`.

    At 03-09 the mixin ships:

    * :meth:`_on_pre_llm_call` (03-10) — recall-policy injection.
    * :meth:`preassemble` (03-09) — ADR-010 Option B override of the
      Hermes ABC method.
    * :meth:`_assemble` (03-09) — shared engine-level wrapper around
      :meth:`ContextAssembler.assemble` (the 03-08 surface).
    * :meth:`_safe_fallback` (03-09) — strip assistant prefill tails.
    * :meth:`_infer_session_id` (03-09) — session-id resolution from
      kwargs / cached session / message metadata / sentinel.
    * :meth:`_apply_assembly_budget_cap` (03-09) — cap a budget against
      ``config.max_assembly_token_budget``.
    * :meth:`_emit_experimental_warning_if_due` (03-09) — rate-limited
      per-turn experimental-mode warning.

    The mixin is on :class:`LCMEngine`'s MRO at issue 02-01 so Epic 03
    can land ``_on_pre_llm_call`` + ``preassemble`` + ``_assemble`` +
    ``_safe_fallback`` bodies without touching :class:`LCMEngine` itself.

    Maps to engine.ts ``assemble`` cluster (lines 6648-6832).
    """

    # ------------------------------------------------------------------
    # Shell-state contract (type-only declarations, no runtime values)
    # ------------------------------------------------------------------
    # Per ADR-027 §Consequences "All state lives on the shell class",
    # these attributes are initialized by :meth:`LCMEngine.__init__`.
    # We re-declare them here as class-level annotations so ``ty`` knows
    # the mixin's methods can rely on them. The actual values come from
    # the shell's ``__init__``.
    if TYPE_CHECKING:
        hermes_home: Path
        config: LcmConfig
        context_length: int
        _conversation_store: Optional[ConversationStore]
        _summary_store: Optional[SummaryStore]
        _session_locks: SessionLockRegistry
        _previous_assembled_messages_by_conversation: Dict[int, Any]
        _stable_orphan_stripping_ordinals_by_conversation: Dict[int, int]
        _has_preassemble: bool
        _experimental_always_on_via_compress: bool
        _last_experimental_warn_ts: float
        ignore_session_patterns: List[re.Pattern[str]]
        # v0.1.2 fix (issue #130, Defect 2). The diff-ingest cursor is
        # initialized on the shell; :meth:`preassemble` resets the
        # per-session entry after a substitution shortens the live
        # list. ``_reset_ingest_cursor_after_compaction`` is owned by
        # the sibling :class:`_CompactMixin` — MRO resolves the
        # ``self.`` call at runtime (the same pattern by which
        # :class:`_CompactMixin.compress` calls ``self._assemble``).
        _last_seen_message_idx: Dict[str, int]

        def _reset_ingest_cursor_after_compaction(
            self,
            *,
            original: List[Dict[str, Any]],
            result: List[Dict[str, Any]],
            session_id: str,
            source: str,
        ) -> None: ...

    # ------------------------------------------------------------------
    # _on_pre_llm_call — recall-policy injection (03-10, unchanged)
    # ------------------------------------------------------------------

    def _on_pre_llm_call(
        self,
        session_id: str = "",
        user_message: Any = None,
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        is_first_turn: bool = False,
        model: str = "",
        platform: str = "",
        sender_id: str = "",
        **kwargs: Any,
    ) -> Optional[Dict[str, str]]:
        """``pre_llm_call`` Hermes hook — recall-policy injection (issue 03-10).

        Per ADR-014: every user turn the engine returns a dict
        ``{"context": LOSSLESS_RECALL_POLICY_PROMPT}`` whose text Hermes
        appends to the current turn's user-message content (NOT the
        system prompt, to preserve the Anthropic prompt cache).

        See the 03-10 PR body for the full rationale and parity-checks
        — this method is unchanged at 03-09. Each turn 03-09 also
        delivers the ALWAYS-ON SUBSTITUTION via :meth:`preassemble`
        (Option B path) or :meth:`_CompactMixin.compress` (Option A
        experimental path) — both run BEFORE this hook fires when
        Hermes 24949 patch lands. The substitution and the recall-
        policy injection are independent.

        Args:
            session_id: The Hermes session identifier.
            user_message: The raw original user message (string or dict).
            conversation_history: Full message history snapshot. ``None``
                is tolerated for forward-compat / partial-kwarg callers.
            is_first_turn: Whether this is the first turn.
            model: The LLM model id.
            platform: The provider platform string.
            sender_id: Gateway platform user id.
            **kwargs: Forward-compat for future hook signature additions.

        Returns:
            ``{"context": LOSSLESS_RECALL_POLICY_PROMPT}``.
        """
        history_len = len(conversation_history) if conversation_history else 0
        logger.debug(
            "[lcm] pre_llm_call inject-policy session=%s history_len=%d first_turn=%s",
            session_id,
            history_len,
            is_first_turn,
        )
        return {"context": LOSSLESS_RECALL_POLICY_PROMPT}

    # ------------------------------------------------------------------
    # preassemble — ADR-010 Option B (production path)
    # ------------------------------------------------------------------

    def preassemble(
        self,
        messages: List[Dict[str, Any]],
        budget_tokens: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Per-turn rewrite hook for the always-on substitution.

        Overrides the Hermes :meth:`ContextEngine.preassemble` ABC
        method (upstream PR #24949). Called by ``run_agent.py``
        BEFORE ``pre_llm_call`` on every turn with the live message
        list and an optional budget. Returns the substituted list.

        ADR-010 §"Path 1": this is the production-mode always-on
        substitution mechanism. The host call site (post-patch) is
        ``run_agent.py:12018``::

            messages = self.context_compressor.preassemble(
                messages, budget_tokens=...
            )

        ### Session-id inference

        Hermes's ``preassemble`` ABC signature does NOT pass a
        ``session_id`` argument — the engine must infer it from the
        message list or a cached recent session id. See
        :meth:`_infer_session_id` for the fallback ladder. When no
        session id can be resolved (early in the conversation, before
        any ingest fires), the engine returns ``messages`` unchanged
        — graceful no-op.

        ### Failure isolation

        The body is wrapped in a try/except that catches ALL exceptions
        and returns ``messages`` unchanged. The substitution path runs
        every turn — a crash here would loop the agent into infinite
        retry. Per the spec's `safe_fallback` invariant, we log the
        error at WARNING and continue with the live messages.

        ### Hermes-less behavior

        In a Hermes-less env (``HERMES_AVAILABLE`` is ``False``), the
        ``ContextEngine`` stub from :mod:`lossless_hermes.hermes_bridge`
        has no :meth:`preassemble` method — but the test path can still
        call this override directly. The body works without any Hermes
        runtime dependency.

        Args:
            messages: Live message list. Hermes passes its in-memory
                conversation_history at API-call time.
            budget_tokens: Token budget for the substitution. ``None``
                falls back to ``self.context_length`` (or
                :data:`_DEFAULT_TOKEN_BUDGET` when context_length is
                still 0 — pre-``update_model``).

        Returns:
            The substituted message list. Always returns a valid list
            (the original ``messages`` on any fallback path); never
            raises.
        """
        try:
            session_id = self._infer_session_id(messages)
            if not session_id:
                # No session id resolvable. ``preassemble`` runs before
                # the first ``post_llm_call``, so on a brand-new session
                # with no ingest history yet there's nothing in the DB
                # to substitute against. Graceful no-op — return the
                # live messages.
                logger.debug(
                    "[lcm] preassemble: no session_id inferred, no-op (messages=%d)",
                    len(messages) if messages else 0,
                )
                return messages

            # Resolve the budget. The host's ``budget_tokens`` arg may
            # be ``None`` (forward-compat: Hermes ABC default is
            # optional) — fall back to ``self.context_length``
            # (populated by ``update_model``), and ultimately to
            # :data:`_DEFAULT_TOKEN_BUDGET` when neither is set (pre-
            # ``update_model`` test paths).
            if (
                budget_tokens is not None
                and isinstance(budget_tokens, (int, float))
                and not isinstance(budget_tokens, bool)
                and budget_tokens > 0
            ):
                effective_budget = int(budget_tokens)
            elif self.context_length and self.context_length > 0:
                effective_budget = int(self.context_length)
            else:
                effective_budget = _DEFAULT_TOKEN_BUDGET

            substituted = self._assemble(
                session_id=session_id,
                messages=messages,
                token_budget=effective_budget,
                prompt=None,
            )

            # v0.1.2 fix (issue #130, Defect 2): when the substitution
            # produced a SHORTER list, reset the diff-ingest cursor to
            # the new length. ``preassemble`` is the Option B (production)
            # path that REPLACES the live message list; without the
            # reset the cursor stays past ``len(substituted)`` and
            # ``_IngestMixin._do_ingest_history_diff`` early-returns
            # forever, silently halting ingest. ``session_id`` is
            # already non-empty here (the ``if not session_id`` guard
            # above returned early). The helper is owned by
            # :class:`_CompactMixin`; ``self.`` resolves it via the MRO.
            self._reset_ingest_cursor_after_compaction(
                original=messages,
                result=substituted,
                session_id=session_id,
                source="preassemble",
            )
            return substituted
        except Exception as err:  # pragma: no cover — wrapper invariant
            # Catch-all: substitution failure on any internal layer
            # MUST NOT crash the agent loop. Log + fall back to live
            # messages. The ``_assemble`` body itself already wraps in
            # a try/except that returns safe_fallback() — this outer
            # catch is a belt-and-suspenders guard for the inference
            # / budget-resolution code above.
            logger.warning(
                "[lcm] preassemble: failed, returning live messages unchanged. error=%s",
                _describe_log_error(err),
            )
            return messages

    # ------------------------------------------------------------------
    # _assemble — shared engine-level wrapper (core substitution body)
    # ------------------------------------------------------------------

    def _assemble(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        token_budget: int,
        prompt: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Engine-level wrapper around :meth:`ContextAssembler.assemble`.

        Handles the policy that lives above the assembler:

        1. Ignored-session bypass → :meth:`_safe_fallback`.
        2. No-conversation lookup → :meth:`_safe_fallback`.
        3. Empty context items → :meth:`_safe_fallback`.
        4. Raw-only items trailing live history → :meth:`_safe_fallback`.
        5. Cache-state-aware orphan-stripping ordinal pinning.
        6. Delegate to :meth:`ContextAssembler.assemble`.
        7. Empty assembled / no-user-turn sanity → :meth:`_safe_fallback`.
        8. Snapshot ``_previous_assembled_messages_by_conversation`` for
           prefix-stability diagnostics.
        9. Pin stable orphan-stripping ordinal on hot-cache turns.
        10. Return assembled messages.

        Wrapped in a try/except so any exception in the body returns
        :meth:`_safe_fallback` instead of crashing the agent loop. The
        substitution mechanism is invisible to the user — a crash here
        would be far worse than a fallback to live messages.

        Acquires the per-session sync lock for the read window so a
        concurrent ingest (post_llm_call) cannot land a partial DB
        write while assembly reads context_items. Per ADR-018 the
        sync surface is the right one (PR #34 sync conversion).

        Maps to ``engine.ts:6648-6832`` (the ``assemble`` body, minus
        the maintenance / telemetry / debug-log sections which are
        Epic 04 territory).

        Args:
            session_id: Resolved session id. The caller (either
                :meth:`preassemble` for Option B or
                :meth:`_CompactMixin.compress` for Option A) is
                responsible for resolving this from kwargs / cache.
            messages: Live in-memory message list. Returned unchanged
                on any fallback path.
            token_budget: Budget the assembler walks under. Capped by
                :meth:`_apply_assembly_budget_cap`.
            prompt: Optional user prompt for BM25-lite-scored
                eviction. ``None`` falls back to chronological.

        Returns:
            The assembled message list (post-sanitize, ready for the
            provider). On any fallback path: the original ``messages``
            list with assistant-prefill tails stripped per
            :meth:`_safe_fallback`.
        """
        # ── Ignored-session bypass ─────────────────────────────────
        # The shell's ``ignore_session_patterns`` is the compiled
        # regex list from ``config.ignore_session_patterns``. Tests
        # often pass empty patterns; production configs ship CI /
        # benchmark session prefixes. If ANY pattern matches the
        # session_id, the engine bypasses the entire substitution
        # path and returns the live messages stripped of assistant
        # prefill tails (TS engine.ts:6666-6668).
        for pattern in self.ignore_session_patterns:
            if pattern.search(session_id):
                logger.debug(
                    "[lcm] assemble: ignore_session_patterns match for session=%s — safe_fallback",
                    session_id,
                )
                return self._safe_fallback(messages)

        # ── Body (wrapped) ────────────────────────────────────────
        # All DB reads + the assembler call live inside this try block
        # so any sqlite / assembler / unicode error returns
        # safe_fallback instead of bubbling up. Mirrors the TS source's
        # outer try/catch at engine.ts:6669-6831.
        try:
            # The lock guards the read window so a concurrent
            # post_llm_call ingest cannot write while we read
            # context_items. Per ADR-018, the sync surface is the
            # right one post-PR #34.
            with self._session_locks.acquire_sync(session_id):
                return self._assemble_locked(
                    session_id=session_id,
                    messages=messages,
                    token_budget=token_budget,
                    prompt=prompt,
                )
        except Exception as err:
            logger.warning(
                "[lcm] assemble: failed for session=%s, returning safe_fallback. error=%s",
                session_id,
                _describe_log_error(err),
            )
            return self._safe_fallback(messages)

    def _assemble_locked(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        token_budget: int,
        prompt: Optional[str],
    ) -> List[Dict[str, Any]]:
        """Locked body of :meth:`_assemble`.

        Factored out so the lock-acquisition try/finally lives in the
        public entry point. All DB reads + the assembler dispatch run
        inside the per-session lock acquired by :meth:`_assemble`.
        """
        # Defensive: if the lifecycle hasn't fired (no
        # ``on_session_start``), the stores are ``None``. Fall back
        # gracefully rather than raising. Production callers always
        # come through Hermes which calls ``on_session_start`` before
        # any LLM turn; test callers that bypass the lifecycle hit
        # this branch.
        if self._conversation_store is None or self._summary_store is None:
            logger.debug(
                "[lcm] assemble: stores not initialized (on_session_start "
                "not called); session=%s — safe_fallback",
                session_id,
            )
            return self._safe_fallback(messages)

        # ── Conversation lookup ───────────────────────────────────
        conversation = self._conversation_store.get_conversation_by_session_id(session_id)
        if conversation is None:
            logger.debug(
                "[lcm] assemble: no conversation for session=%s — safe_fallback",
                session_id,
            )
            return self._safe_fallback(messages)

        conversation_id = conversation.conversation_id

        # ── Token budget cap ─────────────────────────────────────
        effective_budget = self._apply_assembly_budget_cap(token_budget)

        # ── Cache-state-aware orphan-stripping ordinal ─────────────
        # Epic 04 wires the real cache-state machine (reads
        # ``self._telemetry_store.get_conversation_compaction_telemetry``
        # and computes hot/cold from the cache-hit timestamps). For
        # 03-09 we expose a stub :meth:`_resolve_cache_aware_state`
        # that returns "cold" by default — the cold path clears any
        # stable ordinal and the assembler falls back to fresh-tail
        # ordinal. Epic 04 will overwrite the stub with the real
        # state machine and this code path will start honoring hot-
        # cache pinning automatically.
        cache_state = self._resolve_cache_aware_state(conversation_id)
        if cache_state == "hot":
            stable_ordinal: Optional[int] = (
                self._stable_orphan_stripping_ordinals_by_conversation.get(conversation_id)
            )
        else:
            # Cold cache → clear any stale pin and let the assembler
            # use the fresh-tail ordinal directly.
            self._stable_orphan_stripping_ordinals_by_conversation.pop(
                conversation_id,
                None,
            )
            stable_ordinal = None

        # ── Context-items load + empty short-circuit ────────────────
        context_items = self._summary_store.get_context_items(conversation_id)
        if len(context_items) == 0:
            logger.debug(
                "[lcm] assemble: no context items for conversation=%d session=%s — safe_fallback",
                conversation_id,
                session_id,
            )
            return self._safe_fallback(messages)

        # ── Raw-only-trailing-live guard ─────────────────────────
        # Mirrors TS engine.ts:6736-6743. When the DB has only raw
        # context_items AND the count clearly trails the live history,
        # the bootstrap hasn't fully caught up — preserve the live
        # path to avoid dropping prompt context.
        has_summary_items = any(item.item_type == "summary" for item in context_items)
        if not has_summary_items and len(context_items) < len(messages):
            logger.debug(
                "[lcm] assemble: raw-only context trails live history "
                "(items=%d < live=%d) — safe_fallback",
                len(context_items),
                len(messages),
            )
            return self._safe_fallback(messages)

        # ── Assembler dispatch ───────────────────────────────────
        # Deferred import to avoid the circular ``assembler`` →
        # (transitively) ``engine.assemble`` at module load. The
        # circular form only manifests once ``ContextAssembler`` is
        # constructed (it imports nothing from the engine package
        # directly today, but the deferred form future-proofs the
        # arrangement and matches the lifecycle module pattern).
        from lossless_hermes.assembler import AssembleInput, ContextAssembler

        # Construct an assembler. The 03-08 surface is stateless; the
        # cost is trivial (two store references + an optional tz) and
        # constructing per-call removes the need to plumb the assembler
        # instance through the shell's ``__init__`` (which would be a
        # separate epic-02 follow-up). The timezone comes from
        # ``self.config.timezone`` so summary XML attributes match the
        # operator's locale.
        assembler = ContextAssembler(
            conversation_store=self._conversation_store,
            summary_store=self._summary_store,
            timezone=self.config.timezone or None,
        )

        inp = AssembleInput(
            conversation_id=conversation_id,
            token_budget=effective_budget,
            fresh_tail_count=self.config.fresh_tail_count,
            fresh_tail_max_tokens=self.config.fresh_tail_max_tokens,
            prompt=prompt,
            prompt_aware_eviction=self.config.prompt_aware_eviction,
            orphan_stripping_ordinal=stable_ordinal,
            stub_large_tool_payloads=False,
            capture_debug=False,
        )

        assembled: AssembleResult = assembler.assemble(inp)

        # ── Empty / no-user-turn sanity ──────────────────────────
        # Mirrors TS engine.ts:6763-6786. The assembler returned an
        # empty result OR a result with no user turns — either case
        # is a sign the DB is in a half-state that would produce a
        # prefill-rejection error from Anthropic. Fall back to live.
        if len(assembled.messages) == 0 and len(messages) > 0:
            logger.debug(
                "[lcm] assemble: empty assembled output for "
                "conversation=%d session=%s budget=%d — safe_fallback",
                conversation_id,
                session_id,
                effective_budget,
            )
            return self._safe_fallback(messages)

        has_user_turn = any(m.get("role") == "user" for m in assembled.messages)
        if not has_user_turn and len(messages) > 0:
            logger.debug(
                "[lcm] assemble: assembled context has no user turns, "
                "falling back to live to prevent prefill errors "
                "conversation=%d session=%s assembled=%d — safe_fallback",
                conversation_id,
                session_id,
                len(assembled.messages),
            )
            return self._safe_fallback(messages)

        # ── Prefix-stability snapshot ────────────────────────────
        # Save the current assembled message list under the
        # conversation_id so the NEXT turn's assemble can compare
        # via a SHA-256 prefix-stability check. The TS source
        # (engine.ts:6791-6798) uses ``describeAssembledPrefixChange``
        # to compute the common prefix between consecutive turns and
        # logs the divergence; the diagnostic value is the same in
        # Python but Epic 04 wires the log-emission half (this issue
        # just maintains the snapshot map).
        self._previous_assembled_messages_by_conversation[conversation_id] = list(
            assembled.messages
        )

        # ── Hot-cache: pin orphan-stripping ordinal ───────────────
        # When cache state is hot, persist the ordinal the assembler
        # used so the NEXT turn assembles with the SAME orphan-strip
        # boundary, preserving prefix stability and keeping the
        # Anthropic prompt cache hot. The fallback to 0 here is for
        # the empty-debug case (the 03-08 assembler returns
        # ``debug=None`` when ``capture_debug=False``; we used that
        # default for 03-09 since the snapshot logging is Epic 04).
        if cache_state == "hot":
            # Without debug, recompute the ordinal we asked for: if
            # we passed a stable_ordinal, use it; otherwise we let
            # the assembler pick fresh-tail (which we don't have a
            # cheap way to recover here without re-running). For
            # cold cache (where we'd CLEAR the pin anyway), this
            # branch doesn't fire, so the only risk is hot-cache-
            # first-turn — and on that turn stable_ordinal is None,
            # so we end up not pinning. Epic 04 will flip on
            # ``capture_debug`` and pin from
            # ``assembled.debug.orphan_stripping_ordinal``.
            if stable_ordinal is not None:
                self._stable_orphan_stripping_ordinals_by_conversation[conversation_id] = (
                    stable_ordinal
                )

        # ── Done ────────────────────────────────────────────────
        logger.debug(
            "[lcm] assemble: done conversation=%d session=%s "
            "context_items=%d has_summary=%s live=%d assembled=%d "
            "budget=%d estimated=%d",
            conversation_id,
            session_id,
            len(context_items),
            has_summary_items,
            len(messages),
            len(assembled.messages),
            effective_budget,
            assembled.estimated_tokens,
        )
        return assembled.messages

    # ------------------------------------------------------------------
    # _safe_fallback — strip assistant prefill tails
    # ------------------------------------------------------------------

    def _safe_fallback(
        self,
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Return a fresh copy of ``messages`` with assistant prefill tails stripped.

        Mirrors TS ``engine.ts:6658-6664`` (``safeFallback``):

        * Slice the input (return a new list — the gateway's
          ``assembled.messages !== sourceMessages`` reference-equality
          check at the TS callsite distinguishes "engine returned new
          assembled context" from "engine no-op'd" — we preserve the
          semantics by always returning a fresh list).
        * Pop trailing assistant messages — Hermes-side bookkeeping
          may leave behind a partial-prefill assistant entry at the
          end of the list (Anthropic API rejects prefill on the next
          turn if the message ends with ``role=assistant``). Stripping
          them is safe; the model regenerates from the last user turn.

        Returns:
            A new list. Never mutates the input.
        """
        # ``list(messages)`` is the Python equivalent of TS
        # ``params.messages.slice()``. We always return a NEW list so
        # callers that compare by identity (``is``) see the new array.
        msgs: List[Dict[str, Any]] = list(messages or [])
        while msgs and msgs[-1].get("role") == "assistant":
            msgs.pop()
        return msgs

    # ------------------------------------------------------------------
    # _infer_session_id — fallback ladder for preassemble
    # ------------------------------------------------------------------

    def _infer_session_id(
        self,
        messages: Optional[List[Dict[str, Any]]],
    ) -> str:
        """Resolve a session id from the live message list.

        Hermes's ``preassemble`` ABC method does NOT pass ``session_id``
        as an argument (the signature is ``(messages, budget_tokens)``).
        The engine must infer it from one of:

        1. **Message-list metadata.** Some Hermes paths thread a
           ``session_id`` field on individual message dicts (e.g.
           gateway-built conversation_history snapshots). We scan the
           list for the most recent non-empty ``session_id`` /
           ``_session_id`` / ``sender_id`` key.
        2. **Cached most-recent session.** When ``post_llm_call`` fires
           it sets ``self._last_seen_session_id`` (a single string,
           overwritten each turn). If the message list scan finds
           nothing, fall back to this cache.
        3. **Empty sentinel.** No session id resolvable. Returns ``""``
           so the caller (:meth:`preassemble`) can treat as graceful
           no-op (return messages unchanged).

        Note: the 03-09 implementation ships paths 1 and 3. Path 2's
        cache is populated by :meth:`_IngestMixin._on_post_llm_call`
        when that landed for the same engine instance — we read the
        attribute via ``getattr(self, "_last_seen_session_id", "")`` so
        the access is tolerant of engines that haven't fired any
        ingest yet.

        Args:
            messages: The live message list, or ``None``.

        Returns:
            The resolved session id, or ``""`` if none found.
        """
        if not messages:
            cached: str = getattr(self, "_last_seen_session_id", "") or ""
            return cached

        # Path 1: scan messages for a session_id field.
        for msg in reversed(messages):
            if not isinstance(msg, Mapping):
                continue
            for key in ("session_id", "_session_id", "sender_id"):
                val = msg.get(key)
                if isinstance(val, str) and val:
                    return val

        # Path 2: most-recent cached session id from ingest.
        cached_path2: str = getattr(self, "_last_seen_session_id", "") or ""
        if cached_path2:
            return cached_path2

        # Path 3: nothing resolvable — return empty sentinel.
        return ""

    # ------------------------------------------------------------------
    # _apply_assembly_budget_cap — cap budget against config
    # ------------------------------------------------------------------

    def _apply_assembly_budget_cap(self, budget: int) -> int:
        """Cap ``budget`` against ``self.config.max_assembly_token_budget``.

        Mirrors TS ``engine.ts:2118-2122`` (``applyAssemblyBudgetCap``).
        When the config field is set and positive, return
        ``min(budget, cap)``; otherwise return ``budget`` unchanged.

        Args:
            budget: Caller-provided budget. Returned unchanged when no
                cap is configured.

        Returns:
            The capped budget. Always a positive int (caller must
            ensure ``budget > 0``).
        """
        cap = getattr(self.config, "max_assembly_token_budget", None)
        if cap is not None and isinstance(cap, int) and cap > 0:
            return min(budget, cap)
        return budget

    # ------------------------------------------------------------------
    # _resolve_cache_aware_state — Epic 04 stub
    # ------------------------------------------------------------------

    def _resolve_cache_aware_state(self, conversation_id: int) -> str:
        """Stub returning ``"cold"``; Epic 04 wires the real state machine.

        Mirrors TS ``engine.ts:resolveCacheAwareState``. The real body
        reads compaction telemetry and computes hot/cold from the
        Anthropic cache-hit timestamps. Until Epic 04 lands the body
        we conservatively return ``"cold"`` — which:

        * Clears any stale orphan-stripping-ordinal pin on every turn.
        * Falls back to fresh-tail ordinal in the assembler.

        Both behaviors are correctness-preserving. The cost is
        prefix-instability on consecutive turns (the orphan-strip
        boundary may shift), which Epic 04 fixes.

        Args:
            conversation_id: Scope (consumed by Epic 04).

        Returns:
            ``"cold"`` at 03-09. Epic 04 returns ``"hot"`` /
            ``"cold"`` based on real telemetry.
        """
        del conversation_id  # Unused at 03-09.
        return "cold"

    # ------------------------------------------------------------------
    # _emit_experimental_warning_if_due — rate-limited per-turn warning
    # ------------------------------------------------------------------

    def _emit_experimental_warning_if_due(self) -> bool:
        """Emit the experimental-mode warning if the cooldown has elapsed.

        Per ADR-010 §"Path 2" and the issue spec AC line 8, when the
        experimental fallback is active, a warning fires at engine
        init AND on every turn (rate-limited to once per minute).
        :class:`LCMEngine.__init__` emits the init-time warning;
        :class:`_CompactMixin.compress` calls this method on every
        turn the fallback path actually runs.

        Returns ``True`` if the warning was emitted, ``False`` if
        suppressed by the cooldown. Tests use the return value to
        assert the rate-limiting works.
        """
        now = time.monotonic()
        if now - self._last_experimental_warn_ts < _EXPERIMENTAL_WARN_COOLDOWN_S:
            return False
        self._last_experimental_warn_ts = now
        logger.warning(
            "[lcm] always-on substitution via EXPERIMENTAL force-compress "
            "path fired (ADR-010 Option A). This rotates the Hermes session "
            "ID, re-fires memory provider extraction, and trips compression-"
            "count warnings. NOT FOR PRODUCTION. Disable via "
            "`experimental_always_on_via_compress: false` in "
            "$HERMES_HOME/config.yaml."
        )
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_log_error(err: BaseException) -> str:
    """Return a compact one-line description of ``err`` suitable for logs.

    Mirrors TS ``describeLogError`` (engine.ts util). Avoids dumping
    full tracebacks at WARNING level — operators reading scrolling logs
    want the gist; the full traceback lands at DEBUG via
    ``logger.exception``.

    Args:
        err: Any exception.

    Returns:
        ``"<ExceptionTypeName>: <message>"`` — one line, no traceback.
    """
    return f"{type(err).__name__}: {err}"
