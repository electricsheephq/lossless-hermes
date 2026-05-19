# Investigation: PR #24949 (`preassemble`) vs Hermes core PR #7464

**Date:** 2026-05-14
**Investigator:** Claude (parallel-track, while Wave 4 executor agent works mid-build)
**Status:** **PR #24949 is REDUNDANT** — upstream already provides the substitution seam we need.
**Action required:** revise ADR-010, retire `docs/upstream/001-preassemble-abc.md`, clean dead code from `engine/assemble.py` (PR #56 / issue 03-09).

> **Correction (2026-05-19, issue [#137](https://github.com/electricsheephq/lossless-hermes/issues/137), review slice S1):**
> This doc originally concluded the Option-A force-compress path (force
> `should_compress() == True` so `compress()` runs every turn) was
> **"production-grade"**. **That conclusion is wrong and is retracted.**
> Forcing compress every turn rotates the SQLite session ID on every turn —
> `_compress_context` ends the current session and starts a new one inside
> every invocation (`run_agent.py:10311`). A fresh `session_id` per turn fires
> `commit_memory_session(messages)` every turn, so memory providers
> **re-extract from the same conversation N times** (memory re-extraction
> spam), and it also corrupts gateway routing / langfuse lineage and trips the
> compression-count warning in 2–3 turns. This is exactly the breakage
> spike 002 §"Option A" already documented as **NOT shippable**. The
> still-correct findings of this doc are narrower: (1) Hermes core PR #7464
> introduced the `ContextEngine` ABC, whose `compress()` is the upstream
> message-rewrite seam, so our upstream `preassemble` patch (PR #24949) is
> redundant; (2) the `preassemble()` override merged in PR #56 is dead code.
> What is **NOT** correct is calling the *force-every-turn* activation of
> `compress()` "production". The shippable per-turn path is **not** required at
> all — see ADR-032 (issue [#132](https://github.com/electricsheephq/lossless-hermes/issues/132)),
> which supersedes ADR-010 with ingest + threshold/debt-gated compaction
> instead of per-turn substitution. Read every "production" claim below
> through this correction.

---

## TL;DR

Upstream Hermes [PR #7464](https://github.com/NousResearch/hermes-agent/pull/7464) (merged 2026-04-11, commit `79198eb`) introduced the `ContextEngine` ABC with a `compress(messages, current_tokens) -> List[Dict]` method, and `run_agent.py` calls it **before** the LLM API call via a preflight check at `run_agent.py:7565-7606`. That call site IS the always-on substitution seam ADR-010 specified `preassemble` for.

**Implication:** an LCM engine that
1. Returns `True` from `should_compress` every turn (trivially — set `threshold_tokens=0` and the preflight `_preflight_tokens >= threshold_tokens` check fires unconditionally), and
2. Returns the fully-assembled LCM message list from `compress()`,

is **pre-call substituted into every turn** by Hermes core. No `preassemble` ABC method needed.

> **Retracted (issue #137, S1):** the sentence below originally claimed the
> `compress_every_turn` flag is "the production path." It is **not** —
> forcing `compress()` every turn rotates the session ID every turn
> (`run_agent.py:10311`) and spams memory re-extraction. The *seam*
> (`compress()`) is upstream-provided and real; *force-every-turn* is the
> unshippable activation of it. `compress()` is the correct rewrite seam;
> the correct trigger is threshold/debt-gated, not every-turn — see ADR-032.

~~What ADR-010 called the "Option A experimental fallback" (`compress_every_turn` flag) is actually the **production path**.~~

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

### Why `compress()` is the engine API — and why "compress-EVERY-turn" is NOT production-grade

> **Corrected heading (issue #137, S1).** This section originally read "Why
> 'Option A' (compress-every-turn) is actually production-grade" and argued
> force-every-turn was shippable. **That claim is retracted.** The points
> below are split into what holds (the `compress()` *seam* is the upstream
> engine API) and what does **not** (forcing it *every turn* is unshippable).

ADR-010 framed the compress-every-turn path as a hacky fallback gated by an `experimental_always_on_via_compress` config flag, with the assumption that a "real" implementation needs `preassemble`. The `preassemble`-ABC assumption predates PR #7464 and is obsolete — but that does NOT make *force-every-turn* production-grade.

What **holds** post-#7464 — `compress()` is the upstream engine rewrite seam:
- **Officially the engine API** — `compress()` is the only abstract method that returns a new message list.
- **Called pre-API-call** — preflight runs before the LLM dispatch.
- **Used by the built-in compressor** — same path as Hermes's own `ContextCompressor`.

What does **NOT** hold — the *every-turn* activation is not shippable:
- **`threshold_tokens=0` / always-True `should_compress` is a hazard, not a feature.** It makes `_compress_context` fire every turn, and `_compress_context` rotates the SQLite session ID every turn (`run_agent.py:10311`). That triggers `commit_memory_session` every turn → **memory providers re-extract the same conversation N times** (re-extraction spam), corrupts gateway/langfuse session lineage, resets the file-read dedup cache, and trips the "session compressed N times" warning within 2–3 turns. Spike 002 §"Option A" documents this in full and rules it **NOT shippable**.
- `compress` is the right *seam*; the right *trigger* is the engine's real threshold / deferred-compaction-debt gate, **not** every turn. ADR-032 (issue #132) supersedes ADR-010 and adopts ingest + threshold/debt-gated compaction — there is no per-turn-substitution requirement.

## What this means for our work

### ADR-010 ("Always-on assembly emulation")
**Status update:** Proposed → SUPERSEDED.

The decision text needs a follow-up ADR. **That ADR is ADR-032** (issue
[#132](https://github.com/electricsheephq/lossless-hermes/issues/132)), which:
1. Documents PR #7464 as the upstream-provided seam.
2. Removes the "Option A vs Option B" framing.
3. Adopts **ingest + threshold/debt-gated compaction** — per-turn assembly is
   not required.

> **Retracted (issue #137, S1):** items 3–4 of this list originally read
> "Promotes `compress`-every-turn to canonical" and "Documents the
> `threshold_tokens=0` trick … as the activation mechanism." Both are wrong —
> compress-EVERY-turn rotates the session ID every turn and spams memory
> re-extraction (`run_agent.py:10311`). ADR-032 adopts a threshold/debt-gated
> trigger, **not** every-turn substitution. The list above is the corrected
> version.

ADR-010 itself stays in the repo (we don't rewrite ADRs — we supersede them per CLAUDE.md "Don't change an ADR's decision without writing a new ADR that supersedes it").

### PR #24949 (upstream `preassemble` patch)
**Action:** close as obsolete. Comment on it citing PR #7464 superseded the need. Mark `docs/upstream/001-preassemble-abc.md` as **STATUS: closed-as-obsolete**.

### Issue 03-09 / PR #56 (already merged)
The merged code includes a `preassemble()` override on `_AssembleMixin`. Per `engine/assemble.py` header lines 9-24:

> **Production (Option B)** — `preassemble` overrides the Hermes `ContextEngine.preassemble` ABC method (upstream PR #24949). Called every turn by `run_agent.py` BEFORE `pre_llm_call` with the live `messages` list and a budget. Returns the substituted list (or the original on any fallback). This is the path the production v1.0 ships on.
>
> **Experimental (Option A)** — when `preassemble` is ABSENT and `experimental_always_on_via_compress` is True, the engine routes substitution via `_CompactMixin.compress`. [...]

**Both `engine/assemble.py` header statements are wrong** — but **not** in the
direction this doc originally claimed. The accurate position:

- The `preassemble()` override is **dead code** — Hermes never calls it because the upstream ABC has no `preassemble` method. (This bullet was, and remains, correct.)
- The header's "Production (Option B)" claim is wrong because `preassemble` is dead — **but the fix is NOT to promote force-`compress`-every-turn to "production."** Force-every-turn rotates the session ID every turn (`run_agent.py:10311`) and spams memory re-extraction; it is not shippable (spike 002 §"Option A").

> **Retracted (issue #137, S1):** the two bullets that originally followed —
> "The compress-based path is the **production path**, not experimental" and
> "the `experimental_always_on_via_compress` flag should default to `True`" —
> are wrong and are struck. ~~The compress-based path is the production path,
> not experimental.~~ ~~The `experimental_always_on_via_compress` config flag
> should default to `True`.~~ The correct resolution is ADR-032 (issue #132):
> drop the per-turn-substitution model entirely in favour of ingest +
> threshold/debt-gated compaction.

A follow-up issue should:
1. Remove the `preassemble()` method (dead code).
2. Resolve the `experimental_always_on_via_compress` flag and the
   `_emit_experimental_warning_if_due` warning **per ADR-032** — the engine
   does not force `compress()` every turn at all; substitution is
   threshold/debt-gated.
3. Update the `engine/assemble.py` module docstring to ADR-032's model.
4. Update the 03-09 spec at `epics/03-ingest-assembly/03-09-always-on-substitution-hook.md`.

> **Retracted (issue #137, S1):** an earlier version of this list included
> "Override `should_compress` to return `True` unconditionally (or set
> `threshold_tokens = 0`)." ~~Override `should_compress` to return `True`
> unconditionally.~~ That is the every-turn hazard above; it is struck. The
> engine keeps a real `should_compress` gate per ADR-032.

### Risk of acting now vs deferring

> **Corrected (issue #137, S1):** this section originally implied the cleanup
> was cosmetic — "the warning log says experimental but it's not." That framing
> is wrong: the `experimental_always_on_via_compress` path is **correctly
> labelled experimental** because force-`compress`-every-turn really is
> unshippable (session-ID rotation per turn, memory re-extraction spam). The
> real cleanup is not "drop the misleading warning" — it is to adopt ADR-032's
> threshold/debt-gated model and remove the dead `preassemble()` override.

**Acting now:** clean removal of the dead `preassemble()` override in one PR. The experimental warning stays meaningful until the engine is moved to ADR-032's gated-compaction model.

**Deferring to v0.2:** acceptable. The dead `preassemble()` override is harmless (just unused). v0.1.0 ships correctly **because v0.1.0 does not rely on force-every-turn substitution** — see ADR-032. The follow-up is the ADR-032 migration, not a log-message tweak.

I recommend a cleanup issue tracked alongside ADR-032 (issue #132), NOT a hotfix.

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
