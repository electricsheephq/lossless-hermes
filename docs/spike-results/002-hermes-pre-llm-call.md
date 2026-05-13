# Spike 002: Hermes hook semantics for message rewriting

**Status:** PARTIAL — `pre_llm_call` is ADDITIVE-ONLY (cannot rewrite messages); however the `ContextEngine.compress()` extension point can rewrite messages every turn if we force `should_compress() == True`, at the cost of a session-DB rotation per turn (Option A is feasible but messy; Option B — extending the ABC — is cleaner).
**Date:** 2026-05-13
**Confidence:** 95%
**Decision impact:** ADR-001 (LCM-on-Hermes assembly strategy) — informs whether we extend Hermes upstream or hijack `compress()`.

## Question
Does Hermes have a hook that lets a `ContextEngine` plugin REWRITE the message list every turn (not just additively inject context)?

## Method

Read in order, citing file:line:

1. `hermes_cli/plugins.py` — full `VALID_HOOKS` set (lines 128–168), `register_hook` (lines 603–618), `invoke_hook` dispatch (lines 1198–1232).
2. `run_agent.py` — every call site of `invoke_hook("pre_llm_call", …)` (line 12034), `invoke_hook("post_llm_call", …)` (line 15410), `invoke_hook("pre_api_request", …)` (line 12510), `invoke_hook("transform_llm_output", …)` (line 15390).
3. `run_agent.py` lines 12031–12053 (`_plugin_user_context` capture) and lines 12266–12277 (how it's spliced into the API-time user message).
4. `run_agent.py` lines 14841–14851 (production `should_compress` gate) and lines 11958–12017 (preflight gate that also triggers `_compress_context`).
5. `run_agent.py` lines 10237–10413 (`_compress_context` body — calls `self.context_compressor.compress(messages, …)` and rotates the SQLite session ID).
6. `agent/context_engine.py` — full ABC (207 lines): `compress`, `should_compress`, `should_compress_preflight`, lifecycle hooks. No `preassemble` / `transform_messages` / `before_prompt_build` method exists.
7. `plugins/context_engine/__init__.py` lines 199–219 — confirmed `_EngineCollector.register_hook` is an explicit **no-op** (line 212–213): directory-mode context-engine plugins **cannot** register `pre_llm_call` hooks at all. They can only register the engine itself.
8. `agent/memory_provider.py` lines 202–212 — `on_pre_compress` is a MemoryProvider hook (not in `VALID_HOOKS`); returns a STRING that's inlined into the compression summary prompt. Not a message-list rewrite.
9. `website/docs/user-guide/features/hooks.md` lines 504–546 — canonical hook docs. "If the callback returns a dict with a `context` key, or a plain non-empty string, the text is appended to the current turn's user message." Also explicitly: `pre_llm_call` is *"the only hook whose return value is used"* for context injection.
10. `plugins/observability/langfuse/__init__.py` lines 589–620, 870–872 — only real plugin using `pre_llm_call` for context-style work; uses it purely for tracing/observation, never rewrites.
11. Grep for `before_prompt_build|transform_messages|pre_compress` across whole tree: zero hits other than the memory-provider `on_pre_compress` discussed above.

## Hooks inventory (every hook Hermes defines)

`VALID_HOOKS` from `hermes_cli/plugins.py:128–168`. Plus the documented `on_session_*` lifecycle subset.

| Hook name | When fired | kwargs (key ones) | Return-value semantics |
|---|---|---|---|
| `pre_tool_call` | Before every tool execution (`model_tools.py`) | `tool_name`, `args`, `task_id` | `{"action": "block", "message": str}` blocks the tool. Cannot mutate `args`. |
| `post_tool_call` | After every tool returns | `tool_name`, `args`, `result`, `task_id`, `duration_ms` | **Ignored** (observer only). |
| `transform_terminal_output` | In `tools/terminal_tool.py:2058` before terminal output is shown | `command`, `output`, `exit_code`, `cwd` | First non-`None` `str` replaces `output`. |
| `transform_tool_result` | In `model_tools.py:815` after tool dispatch, before result goes back to model | `tool_name`, `arguments`, `result`, `task_id` | First non-`None` `str` replaces `result`. |
| `transform_llm_output` | In `run_agent.py:15390` after the turn produces a final response | `response_text`, `session_id`, `model`, `platform` | First non-empty `str` replaces the response. |
| **`pre_llm_call`** | `run_agent.py:12034` — **once per user turn**, after preflight compression, **before** the tool-calling loop | `session_id`, `user_message`, **`conversation_history` (a COPY: `list(messages)`)**, `is_first_turn`, `model`, `platform`, `sender_id` | **Aggregated APPEND ONLY.** Each non-`None` return is coerced to a string (dict → `r["context"]`); strings joined with `\n\n` and **appended to that turn's user-message content** at API-call time (`run_agent.py:12272–12277`). `conversation_history` is passed as `list(messages)` — a shallow copy — and the original message list is never replaced from the hook's return value. |
| `post_llm_call` | `run_agent.py:15410` — once per turn after final response | `session_id`, `user_message`, `assistant_response`, `conversation_history`, `model`, `platform` | **Ignored** (observer; only persistence side-effects). |
| `pre_api_request` | `run_agent.py:12510` — once per API call inside the tool loop | `task_id`, `session_id`, `message_count`, `tool_count`, `approx_input_tokens`, `request_char_count`, `max_tokens` (NOTE: no `messages` or `api_messages` payload) | **Ignored.** |
| `post_api_request` | After each API response | response metadata | **Ignored** by `run_agent.py`; used by langfuse for tracing. |
| `on_session_start` / `on_session_end` / `on_session_finalize` / `on_session_reset` | Session lifecycle boundaries | `session_id`, `platform`, etc. | **Ignored.** |
| `subagent_stop` | Subagent completion | `parent_session_id`, `child_status`, `duration_ms`, `child_summary` | **Ignored.** |
| `pre_gateway_dispatch` | Gateway pre-dispatch (BEFORE agent runs) | `event`, `gateway`, `session_store` | First recognised action-dict wins: `skip`, `rewrite` (replaces `event.text`), `allow`. **Not on the in-flight LLM path.** |
| `pre_approval_request` / `post_approval_response` | Dangerous-command approval flow | `command`, `pattern_key`, `surface`, `choice` | **Ignored** (observer only). |

Hooks that DON'T exist (verified by grep across the whole tree): `before_prompt_build`, `transform_messages`, `pre_compress` (only `on_pre_compress` as a memory-provider method), `preassemble`.

## The key question: can LCM rewrite messages?

- **Direct answer:** **NO via `pre_llm_call`.** The hook is structurally additive: it captures plugin return values (dict-with-`context` or plain string), joins them with `\n\n`, and appends the joined string to the **current turn's user-message content at API-call time only**. The `conversation_history` kwarg is passed as `list(messages)` (a shallow copy at `run_agent.py:12038`), so mutating it in-place is also a no-op against the real `messages` list. There is no hook anywhere in `VALID_HOOKS` whose return value replaces the in-flight message list.
- **Best mechanism found:** **`ContextEngine.compress()`** — the engine ABC method that already takes `messages: List[Dict]` and returns the replacement list (`agent/context_engine.py:77–96`). `run_agent.py:14841` calls `_compressor.should_compress(_real_tokens)` after each API response and `run_agent.py:11971` runs a parallel preflight check before each turn; either path calls `self._compress_context(...)` → `self.context_compressor.compress(messages, ...)` and the return value REPLACES the live `messages` list (`run_agent.py:10264, 10413`).
- **Reference file:line:** `run_agent.py:14841` (production check), `run_agent.py:11971` (preflight check), `run_agent.py:10264` (engine call), `agent/context_engine.py:77` (ABC contract).
- **Example code pattern (Option A — force compress every turn):**

  ```python
  # in lossless-hermes context_engine plugin
  from agent.context_engine import ContextEngine

  class LosslessEngine(ContextEngine):
      name = "lossless-hermes"

      def should_compress(self, prompt_tokens: int = None) -> bool:
          # Force the assembly path every turn so compress() runs every turn.
          return True

      def should_compress_preflight(self, messages) -> bool:
          # Belt-and-suspenders: also fire on the pre-turn path.
          return True

      def compress(self, messages, current_tokens=None, focus_topic=None):
          # LCM's always-on assembly: replace evicted raw turns with summary stubs.
          return self._assemble(messages)
  ```

  Plus `plugin.yaml`-style `register(ctx)` calling `ctx.register_context_engine(LosslessEngine())` (NOT `register_hook` — that's the no-op in `_EngineCollector`).

## The `pre_llm_call` snippet (for the record)

```python
# run_agent.py:12031–12053  (verbatim, key lines)
_plugin_user_context = ""
try:
    from hermes_cli.plugins import invoke_hook as _invoke_hook
    _pre_results = _invoke_hook(
        "pre_llm_call",
        session_id=self.session_id,
        user_message=original_user_message,
        conversation_history=list(messages),       # ← COPY, not the live list
        is_first_turn=(not bool(conversation_history)),
        model=self.model,
        ...
    )
    _ctx_parts: list[str] = []
    for r in _pre_results:
        if isinstance(r, dict) and r.get("context"):
            _ctx_parts.append(str(r["context"]))
        elif isinstance(r, str) and r.strip():
            _ctx_parts.append(r)
    if _ctx_parts:
        _plugin_user_context = "\n\n".join(_ctx_parts)
```

And the injection site (`run_agent.py:12266–12277`):

```python
if idx == current_turn_user_idx and msg.get("role") == "user":
    _injections = []
    if _ext_prefetch_cache:
        _fenced = build_memory_context_block(_ext_prefetch_cache)
        if _fenced:
            _injections.append(_fenced)
    if _plugin_user_context:
        _injections.append(_plugin_user_context)
    if _injections:
        _base = api_msg.get("content", "")
        if isinstance(_base, str):
            api_msg["content"] = _base + "\n\n" + "\n\n".join(_injections)
```

The injection is into a **copy** (`api_msg`), and only on the current turn's user message. So `pre_llm_call` cannot retroactively remove or restate older turns — exactly the opposite of what LCM's "every-turn substitution of evicted turns with summary stubs" requires.

## Alternative routes if `pre_llm_call` doesn't support rewrite

### Option A: Force `should_compress` to always return True; do substitution inside `compress()`

**Feasibility:** Works mechanically. `_compress_context()` calls `self.context_compressor.compress(messages, …)` and assigns the result back to `messages`. We control both `should_compress` (returns True) and `compress` (returns the assembled list).

**Trade-offs / costs:**
- **Session-ID rotation every turn.** `run_agent.py:10311–10337` ends the current SQLite session and starts a new one **inside every `_compress_context` invocation**. Forcing compress-every-turn produces a fresh `session_id` per turn — gateway routing, memory provider lineage, langfuse traces, and `parent_session_id` chains all assume sessions only rotate on real compression. This is a serious correctness hazard.
- **`commit_memory_session(messages)` fires every turn** (`run_agent.py:10309`) — memory providers will re-extract from the same conversation N times.
- **Compression-count warning at run_agent.py:10380** ("Session compressed N times — accuracy may degrade. Consider /new to start fresh.") will trip in 2–3 turns.
- **File-read dedup cache reset every turn** (`run_agent.py:10402–10406`) — model will re-read files it just read.
- **`logger.info("context compression done…")` on every turn** — log spam.
- **No control over preflight ordering:** preflight uses raw `_preflight_tokens >= threshold_tokens` (`run_agent.py:11971`), NOT `should_compress_preflight`. To make it fire every turn we'd need `threshold_tokens = 0` — that's another invariant we'd be violating that other code might key off.

**Verdict:** Possible as a hack to validate the LCM assembly algorithm in isolation, but **NOT shippable** — it breaks session lifecycle, memory provider lineage, and observability.

### Option B: Upstream ABC patch adding `preassemble(messages, budget) → messages` method

**Shape:**

```python
# agent/context_engine.py — add to ContextEngine ABC
def preassemble(self, messages: List[Dict[str, Any]], budget_tokens: int = None) -> List[Dict[str, Any]]:
    """Optional per-turn assembly hook. Called BEFORE pre_llm_call, AFTER
    preflight compression. Engines that maintain an always-on substitution
    invariant (e.g. LCM) override this to rewrite the message list every
    turn. Default returns messages unchanged."""
    return messages
```

Then in `run_agent.py` around line 12018 (after preflight compression, before `pre_llm_call`):

```python
if hasattr(self.context_compressor, "preassemble"):
    messages = self.context_compressor.preassemble(messages, budget_tokens=...)
```

**Effort:** ~30 LOC: one ABC method, one call site, one test confirming default-no-op + override-replaces. No session rotation, no DB writes, no log spam.

**Trade-offs:**
- Requires an upstream PR to Hermes (one method addition to a public ABC is low-risk + non-breaking — default returns input unchanged).
- Gives LCM a clean shippable extension point AND doesn't conflict with the existing `compress()` semantics (compress fires when over threshold, preassemble fires every turn).

**Verdict:** **Cleanest path.** Strongly preferred.

### Option C: Run inside an existing pre-LLM choke point that already mutates messages

There's a single pre-turn choke point that already rewrites `messages` in place: the preflight-compression loop at `run_agent.py:11958–12017`. It's gated by `_preflight_tokens >= self.context_compressor.threshold_tokens`. If we set `threshold_tokens = 0` in our engine (via `update_model` override), preflight fires every turn and calls our `compress()` — same as Option A, but routed through the preflight rather than the post-response path.

**Difference from A:** This avoids the post-response check at `run_agent.py:14841` because the preflight call invokes `_compress_context` directly. But preflight ALSO calls `_compress_context`, which still rotates the session. **Same root problem.**

**Verdict:** Worse than A (less idiomatic), same trade-offs. Skip.

### Option D: Hijack `should_compress_preflight` (which exists but is unused on the hot path)

`ContextEngine.should_compress_preflight(messages)` is defined in the ABC (`agent/context_engine.py:100–106`, default `False`) but **never called** by `run_agent.py` — grep returns zero call sites in `run_agent.py`. So overriding it doesn't help.

**Verdict:** Dead code as a hook surface. Skip.

## Recommendation

**Pursue Option B.** Open an upstream PR to Hermes adding `ContextEngine.preassemble(messages, budget) -> messages` (default no-op). The implementation is ~30 LOC, the default returns input unchanged so it's non-breaking, and it gives the LCM port the precise per-turn substitution hook LCM v4.1's win depends on without the session-rotation side effects of Options A/C.

If upstream is blocked, **fall back to Option A** but document the breakage explicitly (session rotation per turn, memory provider lineage corruption, compression-count noise). Tag the release as "experimental — requires Hermes upstream patch for production use."

## Remaining 5% risk

- I did NOT run the engine end-to-end to confirm the call ordering empirically — analysis is pure source-read. A unit test that wires a stub engine with `should_compress=True` and observes message-list replacement would close this gap (~30 min of work).
- I did NOT investigate whether `_memory_manager.on_pre_compress` plays a useful role in the always-on flow — it could either help (preserve insights pre-eviction) or hurt (fire every turn alongside our substitution). Worth a follow-up read of `agent/memory_manager.py:438–460` before locking the design.
- I confirmed `pre_llm_call` callbacks operate on a COPY of `messages`, but did NOT confirm that mutating the COPY has zero downstream effect — there is one Python edge case where shallow-copy + dict mutation could leak (each message dict is shared by reference even though the list is copied). If a plugin mutated `m["content"]` in-place on `conversation_history`, that WOULD leak into the live messages. The official return-value contract is the intended path; this would be a "clever hack" path, but it's worth noting that the encapsulation isn't airtight. Not a real LCM rewrite vector (we want to add/remove messages, not mutate existing ones), but worth flagging for the ADR.
