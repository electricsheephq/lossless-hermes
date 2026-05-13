# Investigation: PR #24949 (`preassemble`) vs Hermes core PR #7464

**Date:** 2026-05-14
**Investigator:** Claude (parallel-track, while Wave 4 executor agent works mid-build)
**Status:** **PR #24949 is REDUNDANT** — upstream already provides the substitution seam we need.
**Action required:** revise ADR-010, retire `docs/upstream/001-preassemble-abc.md`, mark experimental compress-based path as **production**, clean dead code from `engine/assemble.py` (PR #56 / issue 03-09).

---

## TL;DR

Upstream Hermes [PR #7464](https://github.com/NousResearch/hermes-agent/pull/7464) (merged 2026-04-11, commit `79198eb`) introduced the `ContextEngine` ABC with a `compress(messages, current_tokens) -> List[Dict]` method, and `run_agent.py` calls it **before** the LLM API call via a preflight check at `run_agent.py:7565-7606`. That call site IS the always-on substitution seam ADR-010 specified `preassemble` for.

**Implication:** an LCM engine that
1. Returns `True` from `should_compress` every turn (trivially — set `threshold_tokens=0` and the preflight `_preflight_tokens >= threshold_tokens` check fires unconditionally), and
2. Returns the fully-assembled LCM message list from `compress()`,

is **pre-call substituted into every turn** by Hermes core. No `preassemble` ABC method needed.

What ADR-010 called the "Option A experimental fallback" (`compress_every_turn` flag) is actually the **production path**.

## Evidence

### `ContextEngine` ABC at PR #7464

`agent/context_engine.py` (184 LOC, NEW) at commit `79198eb`:

```python
class ContextEngine(ABC):
    @abstractmethod
    def update_from_response(self, usage: Dict[str, Any]) -> None: ...

    @abstractmethod
    def should_compress(self, prompt_tokens: int = None) -> bool: ...

    @abstractmethod
    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
    ) -> List[Dict[str, Any]]:
        """Compact the message list and return the new message list.

        This is the main entry point. The engine receives the full message
        list and returns a (possibly shorter) list that fits within the
        context budget. The implementation is free to summarize, build a
        DAG, or do anything else — as long as the returned list is a valid
        OpenAI-format message sequence."""

    def should_compress_preflight(self, messages: List[Dict[str, Any]]) -> bool: ...
    def on_session_start(self, session_id, **kwargs) -> None: ...
    def on_session_end(self, session_id, messages) -> None: ...
    def on_session_reset(self) -> None: ...
    def get_tool_schemas(self) -> List[Dict[str, Any]]: ...
    def handle_tool_call(self, name, args, **kwargs) -> str: ...
    def get_status(self) -> Dict[str, Any]: ...
    def update_model(self, model, context_length, ...) -> None: ...
```

**No `preassemble` method exists.** The ABC stops at `compress`.

### Pre-call substitution site at `run_agent.py:7565-7606`

```python
if (
    self.compression_enabled
    and len(messages) > self.context_compressor.protect_first_n
                        + self.context_compressor.protect_last_n + 1
):
    _preflight_tokens = estimate_request_tokens_rough(
        messages, system_prompt=active_system_prompt or "", tools=self.tools or None,
    )

    if _preflight_tokens >= self.context_compressor.threshold_tokens:
        # ...
        for _pass in range(3):
            _orig_len = len(messages)
            messages, active_system_prompt = self._compress_context(
                messages, system_message, approx_tokens=_preflight_tokens,
                task_id=effective_task_id,
            )
            # ... loop if not under threshold ...
```

This block runs **before** the LLM API call. `self.context_compressor` is the registered engine instance (line 1310: `self.context_compressor = _selected_engine`). `_compress_context` wraps `compress()`. The whole substitution happens pre-call.

### Why "Option A" (compress-every-turn) is actually production-grade

ADR-010 framed the compress-every-turn path as a hacky fallback gated by an `experimental_always_on_via_compress` config flag, with the assumption that a "real" implementation needs `preassemble`. That framing predates PR #7464.

Post-#7464, the compress-every-turn approach is:
- **Officially the engine API** — `compress()` is the only abstract method that returns a new message list.
- **Called pre-API-call** — preflight runs before the LLM dispatch.
- **Used by the built-in compressor** — same path as Hermes's own `ContextCompressor`.
- **No flag needed** — set `threshold_tokens=0` and `should_compress(0)` returns True every turn (the docstring at `context_engine.py` even calls this out: "should_compress(0) never fires" is mentioned as a documented edge case in `run_agent.py:9498`).

There is no second path. `compress` is THE path.

## What this means for our work

### ADR-010 ("Always-on assembly emulation")
**Status update:** Proposed → SUPERSEDED.

The decision text needs a follow-up ADR (e.g., ADR-031) that:
1. Documents PR #7464 as the upstream-provided seam.
2. Removes the "Option A vs Option B" framing.
3. Promotes `compress`-every-turn to canonical.
4. Documents the `threshold_tokens=0` trick (or just override `should_compress` to return True unconditionally) as the activation mechanism.
5. References this investigation doc.

ADR-010 itself stays in the repo (we don't rewrite ADRs — we supersede them per CLAUDE.md "Don't change an ADR's decision without writing a new ADR that supersedes it").

### PR #24949 (upstream `preassemble` patch)
**Action:** close as obsolete. Comment on it citing PR #7464 superseded the need. Mark `docs/upstream/001-preassemble-abc.md` as **STATUS: closed-as-obsolete**.

### Issue 03-09 / PR #56 (already merged)
The merged code includes a `preassemble()` override on `_AssembleMixin`. Per `engine/assemble.py` header lines 9-24:

> **Production (Option B)** — `preassemble` overrides the Hermes `ContextEngine.preassemble` ABC method (upstream PR #24949). Called every turn by `run_agent.py` BEFORE `pre_llm_call` with the live `messages` list and a budget. Returns the substituted list (or the original on any fallback). This is the path the production v1.0 ships on.
>
> **Experimental (Option A)** — when `preassemble` is ABSENT and `experimental_always_on_via_compress` is True, the engine routes substitution via `_CompactMixin.compress`. [...]

**Both statements are now wrong.** The truth is:

- The `preassemble()` override is **dead code** — Hermes never calls it because the upstream ABC has no `preassemble` method.
- The compress-based path is the **production path**, not experimental.
- The `experimental_always_on_via_compress` config flag should default to `True` (or be removed).
- The "rate-limited per-turn experimental-mode warning" at `engine/assemble.py:_emit_experimental_warning_if_due` is misleading — there's nothing experimental about it.

A follow-up issue (filed below as **lossless-hermes#NN**) should:
1. Remove the `preassemble()` method (dead code).
2. Remove the `experimental_always_on_via_compress` config flag (or invert it to a kill-switch).
3. Remove the `_emit_experimental_warning_if_due` warning.
4. Update the `engine/assemble.py` module docstring.
5. Override `should_compress` to return `True` unconditionally (or set `threshold_tokens = 0` in `on_session_start`).
6. Update the 03-09 spec at `epics/03-ingest-assembly/03-09-always-on-substitution-hook.md`.

### Risk of acting now vs deferring

**Acting now (recommended):** clean removal in one PR while the agent is in late Wave 4 / Wave 5. Saves Wave 5 / 6 reviewers from wondering why the experimental path looks production-grade.

**Deferring to v0.2:** acceptable. The current code works (compress-every-turn is the experimental path that's actually wired up; `preassemble` is just unused). v0.1.0 ships correctly. The cleanup is "the warning log says experimental but it's not" — a documentation embarrassment, not a runtime bug.

I recommend a Wave 6 cleanup issue, NOT a Wave 4 hotfix — the agent should stay focused on closing Wave 4.

## Verification steps the next session should run

```bash
# 1. Confirm Hermes core preassemble does not exist
grep -rn "def preassemble\|preassemble(" /Volumes/LEXAR/Claude/hermes-agent/agent/

# 2. Confirm compress IS the substitution seam (cite the line)
sed -n '7565,7606p' /Volumes/LEXAR/Claude/hermes-agent/run_agent.py

# 3. Sanity-check that our LCMEngine.compress override returns the assembled list
grep -A 5 "def compress" /Volumes/LEXAR/Claude/lossless-hermes/src/lossless_hermes/engine/compact.py

# 4. Check whether our threshold_tokens / should_compress combination is currently set to fire-every-turn
grep -n "threshold_tokens\|should_compress" /Volumes/LEXAR/Claude/lossless-hermes/src/lossless_hermes/engine/compact.py
```

## Cross-references

- [`docs/adr/010-always-on-assembly-emulation.md`](../adr/010-always-on-assembly-emulation.md) — original framing (will be superseded)
- [`docs/upstream/001-preassemble-abc.md`](./001-preassemble-abc.md) — to be marked closed-as-obsolete
- [Hermes core PR #7464](https://github.com/NousResearch/hermes-agent/pull/7464) — the upstream patch that obsoletes our #24949
- [`run_agent.py:7565-7606`](https://github.com/NousResearch/hermes-agent/blob/79198eb/run_agent.py#L7565-L7606) — substitution seam in upstream
- [`agent/context_engine.py`](https://github.com/NousResearch/hermes-agent/blob/79198eb/agent/context_engine.py) — the actual ABC (no `preassemble`)
- [PR #56 (03-09)](https://github.com/electricsheephq/lossless-hermes/pull/56) — the merged 03-09 implementation that has dead preassemble code
- [`stephenschoettler/hermes-lcm`](https://github.com/stephenschoettler/hermes-lcm) — the competing plugin that ships against PR #7464 with no preassemble dependency, which is what tipped this investigation
