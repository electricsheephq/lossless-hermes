---
patch_id: 003
adr: 015
status: drafted
pr_url: null
last_checked: 2026-05-13
fallback: ADR-009 Option B — diff-on-each-turn against conversation_history from post_llm_call hook (works today; slightly higher latency than per-append)
blocks_issues: []
---

# Upstream patch 003 — `ContextEngine.ingest()` ABC method + run_agent call sites

## Summary

Add an additive `ingest(message)` method to `ContextEngine` ABC (default no-op) and call it from `run_agent.py` at every message-append site (~25 places identified during [spike 002](../spike-results/002-hermes-pre-llm-call.md)).

## Rationale

LCM's v4.1 stores every raw message in its `messages` table immediately at ingest — that's the bedrock for FTS5 search, embedding queue enqueue, entity-extraction queue, and per-message token tracking. Hermes today has no per-message hook; the closest signal is `post_llm_call` which fires once per turn with `conversation_history=list(messages)`.

[ADR-009](../adr/009-per-message-ingest.md) documents the chosen path: ship lossless-hermes with the Option B fallback (diff-on-each-turn) **and** propose this upstream cleanup. With `ingest()` upstream, LCM can drop the diff-tracking state and rely on per-message callbacks.

## Proposed API

```python
# agent/context_engine.py
class ContextEngine(ABC):
    def ingest(self, message: dict[str, Any]) -> None:
        """Per-message ingest hook fired once for each new message appended.

        Called for user messages, assistant responses (including tool_use blocks),
        and tool result messages — in append order. Default no-op.

        Engines that maintain per-message state (e.g., LCM's `messages` table,
        embedding queue, entity-extraction queue) override this. Engines that
        only care about aggregate token usage (e.g., ContextCompressor) ignore it.

        Args:
            message: the message dict in OpenAI format (role, content, tool_calls, etc.)
        """
        pass
```

## Call sites in `run_agent.py`

Add `self.context_engine.ingest(msg)` immediately after each `messages.append(msg)` call (~25 sites). Reference list to be assembled from [spike 002](../spike-results/002-hermes-pre-llm-call.md) findings during PR drafting.

## Why this is acceptable upstream

- **Additive only.** Default no-op. Zero impact on `ContextCompressor` or any engine that doesn't override.
- **Symmetric with existing surface.** `update_from_response(usage)` already fires after each LLM API call; `ingest(message)` extends the same pattern to message appends.
- **Removes a workaround.** Plugin engines today have to diff-track their own snapshot of the message list; this method makes per-message visibility a first-class contract.

## Fallback if rejected

ADR-009 Option B (the v0.1.0 fallback): register `post_llm_call` hook, receive `conversation_history=list(messages)` every turn, diff against `last_seen_message_idx`. Slightly higher latency (per-turn batch rather than per-append), but works today with no Hermes changes.

## Transition log

- **2026-05-13 — drafted.**
- _(future)_ filed → under_review → accepted/rejected
