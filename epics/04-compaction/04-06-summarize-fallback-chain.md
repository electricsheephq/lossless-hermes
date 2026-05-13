---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-04] summarize: port fallback model chain + auth-failure detection'
labels: 'port'
---

## Source (TypeScript)
- File: `lossless-claw/src/summarize.ts` (pr-613 `1f07fbd`)
- Lines:
  - `summarizeWithEscalation`: 1334–1443 (~110 LOC) — owner of the escalation cascade
  - Main retry loop: 1295–1685 (~390 LOC)
  - `attemptSummarizerCall`: ~300–500 (called inside main loop)
  - `retryWithoutModelAuth`: 1374–1449 (~76 LOC)
  - `resolveSummaryCandidates`: 1131–1250 (~120 LOC)
  - `extractProviderAuthFailure`: 525–560 (~36 LOC)
  - `normalizeCompletionSummary`: 319–~440 (~120 LOC, includes envelope-aware extraction)
  - `extractIncompleteResponseSignals`: ~440–480
  - `withTimeout`: 153–161
  - `resolveTargetTokens`: 855–873
  - Error classes: `LcmProviderAuthError`, `LcmProviderResponseError`, `SummarizerTimeoutError`
- Function(s)/class(es): `LcmSummarizer.summarize`, `_summarize_with_escalation`, `_attempt_summarizer_call`, `_retry_without_model_auth`, `_resolve_summary_candidates`, `_extract_provider_auth_failure`, `_normalize_completion_summary`

## Target (Python)
- File: `src/lossless_hermes/summarize.py`
- Estimated LOC: ~1000 (the fallback chain is most of summarize.ts's complexity)
- Class: `LcmSummarizer`
- Public method: `summarize(text, aggressive=False, options=None) → str`
- Internal: `_summarize_with_escalation`, `_attempt_summarizer_call`, `_retry_without_model_auth`, `_resolve_summary_candidates`, `_normalize_completion_summary`, etc.
- Error classes: `LcmProviderAuthError`, `LcmProviderResponseError`, `SummarizerTimeoutError`

## Algorithm

Per `docs/porting-guides/assembler-compaction.md` §"Retry / circuit-breaker / fallback chain":

### 5-layer provider candidate resolution

`_resolve_summary_candidates()` produces an ordered list of `(provider, model)` tuples, deduped, tried in order:

1. Env vars (`LCM_SUMMARY_MODEL` + `LCM_SUMMARY_PROVIDER`)
2. Plugin config (`config.plugins["lossless-hermes"].config.summary_model` + `.summary_provider`)
3. `agents.defaults.compaction.model`
4. `agents.defaults.model`
5. Legacy runtime/session model

Plus appended: explicit fallback providers from `config.fallback_providers[]`.

Each candidate resolved via `deps.resolve_model` (or Hermes equivalent — for Hermes this is just config-driven). Final list: `_dedupe_resolved_candidates`.

**For Hermes port:** the 5-layer resolution maps to Hermes's `auxiliary.<task>` config + a new `lcm_summary_fallbacks: [{provider, model}]` field. See `docs/porting-guides/assembler-compaction.md` §"ADR: Summarizer LLM client" — Option 3 (hybrid).

### Main escalation cascade (`_summarize_with_escalation`)

```
source_text → normal mode → if output_tokens ≥ input_tokens → aggressive mode
                                                            → if output_tokens ≥ input_tokens → deterministic fallback
```

The "didn't compress" guard is anti-thrashing guard #3 from issue 04-04.

Hard cap enforcement (lines 1428–1440): if `summary_tokens > target_tokens * summary_max_overage_factor`, call `_cap_summary_text` (lines 101–122) which appends diagnostic suffixes (`[Capped from N tokens to ~M]`, etc.) and truncates. Level becomes `"capped"`.

### Per-candidate retry loop

For each candidate in `_resolve_summary_candidates()`:

1. **Initial call** — `_attempt_summarizer_call("initial")`. On `requireStructuralSignal` auth failure (HTTP 401 OR explicit `error.kind === "provider_auth"`), trigger `_retry_without_model_auth`: warn, call `get_api_key` with `skip_model_auth=True`, retry. If still auth-failing or runtime-managed, throw `LcmProviderAuthError`.

2. On `LcmProviderAuthError` from the candidate → log "PROVIDER FALLBACK", apply **exponential backoff** `min(500 * 2**index, 8000) ms`, move to next candidate.

3. On `LcmProviderResponseError` (explicit 4xx/5xx, finish=`error|failed|cancelled`, or non-auth `error.kind`) → warn, backoff, move to next candidate.

4. On `SummarizerTimeoutError` (default 60s timeout via `_with_timeout`) → log "timed out", backoff, move to next. If no more candidates AND it was a timeout → return `_build_deterministic_fallback(...)`.

5. On success → **normalize** via `_normalize_completion_summary` (collects text-like fields, dedupes exact fragments preserving first-seen order, drops reasoning/thinking blocks).

6. If empty after content extraction → try **envelope-aware extraction** (`_normalize_completion_summary(result)` against the full envelope, not just `result.content`).

7. If still empty OR `_extract_incomplete_response_signals` non-empty → **retry once** with `reasoning: "low"` (a more conservative call). Retry empty → next candidate; retry succeeded → log "retry succeeded".

8. After all candidates fail and a final `last_auth_error` exists → throw it. Else → return `_build_deterministic_fallback(...)`.

### Auth-failure detection (`_extract_provider_auth_failure`)

Two modes per the porting guide:

- **`require_structural_signal=True`** — used on success-path responses. Only triggers on HTTP 401 OR explicit `error.kind === "provider_auth"`. Plain text matches in the response body are NOT sufficient (an LLM summary may legitimately discuss auth errors).

- **`require_structural_signal=False`** (default) — used on caught errors. Triggers on 401, scope signals (`model.request`, `missing scope`, `insufficient scope`), or `AUTH_ERROR_TEXT_PATTERN` (401, unauthorized, invalid token/api key, etc.).

### Target-token resolution (`_resolve_target_tokens`)

```python
def _resolve_target_tokens(input_tokens: int, mode: str, is_condensed: bool, config) -> int:
    if is_condensed:
        return max(512, config.condensed_target_tokens)
    if mode == "normal":
        return max(192, min(config.leaf_target_tokens, int(input_tokens * 0.35)))
    # mode == "aggressive"
    aggressive_cap = max(96, min(config.leaf_target_tokens, int(config.leaf_target_tokens * 0.55)))
    return max(96, min(aggressive_cap, int(input_tokens * 0.20)))
```

### Sync timeout pattern (per ADR-017)

LCM uses `withTimeout` (Promise.race + sleep). Python port is sync, so we use:

```python
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

def _with_timeout(callable, timeout_s: float):
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(callable)
        try:
            return future.result(timeout=timeout_s)
        except FuturesTimeoutError:
            raise SummarizerTimeoutError(...)
```

Per ADR-017 §Consequences:

> The only place async genuinely helps is `summarize` (LLM timeouts) — but `asyncio.wait_for` is awkward inside sync code. Use `concurrent.futures.ThreadPoolExecutor` + `Future.result(timeout=...)` instead.

## Reasoning-parameter caveat

Per `docs/porting-guides/assembler-compaction.md` §Remaining-5%-risk #4:

> Hermes's `call_llm` doesn't expose a `reasoning` parameter. LCM's retry calls `attemptSummarizerCall("retry", "low")` — passes `reasoning: "low"` to `deps.complete`. Hermes's `auxiliary_client.call_llm` (line 4088) does NOT have a `reasoning` argument. Either pipe it through `extra_body={"reasoning_effort": "low"}` (OpenAI-style) or accept that the conservative retry just retries with the same settings (lower-impact but still useful for transient overload).

Pick `extra_body={"reasoning_effort": "low"}` and pass it through Hermes's LLM client. If Hermes doesn't relay `extra_body` to the provider, fall back to same-settings retry and document the degradation in the issue PR description.

## Wave-N fixes to preserve

Per ADR-029, add inline comment at:

- **Wave-4 P0 deterministic marker invariant** in `_build_deterministic_fallback` (covered in issue 04-05) — referenced again here as the fallback path returns this value.
- **Auth-short-circuit** at the `LcmProviderAuthError` exhaustion path: when all candidates auth-fail, RAISE the error (don't fall to deterministic) — caller (compaction `_leaf_pass`) catches it and sets `auth_failure=True` on `CompactionResult` so the DAG is not corrupted by persisting a fallback summary during a transient outage.
  ```python
  # LCM auth-short-circuit: if all candidates auth-fail, raise rather than
  # returning deterministic fallback. Caller skips persistence to preserve
  # DAG integrity through transient provider outages.
  # Original: lossless-claw/src/summarize.ts:1665–1685 (final-throw path).
  ```

## Dependencies
- Depends on: Issue 04-05 (prompt templates — needed for the actual call body)
- Depends on: Issue 04-04 (anti-thrashing guard #3 — escalation cascade)
- Depends on: Epic 02 (LLM client shim — `agent.llm_client` or Hermes's `auxiliary_client.call_llm` wrapper)
- Blocks: Issue 04-07 (circuit breaker counts failures from this code path)
- Blocks: Issue 04-02 (`_leaf_pass` calls `_summarize_with_escalation` — but the call signature is settled here, port order is concurrent)

## Acceptance criteria
- [ ] 5-layer candidate resolution returns the correct ordered list for a given config (test each layer in isolation)
- [ ] Candidates are deduped on `(provider, model)` tuple (no duplicates in final list)
- [ ] Explicit `fallback_providers[]` are appended (NOT replacing) the 5-layer chain
- [ ] Exponential backoff: `min(500 * 2**index, 8000)` ms — capped at 8000
- [ ] `_retry_without_model_auth` triggers ONLY on `require_structural_signal=True` failures (HTTP 401 OR explicit `error.kind="provider_auth"`)
- [ ] `_retry_without_model_auth` is SKIPPED if `is_runtime_managed_auth_provider(provider)` is True (OAuth-managed providers cannot use `skip_model_auth`)
- [ ] On all-candidate auth failure, `LcmProviderAuthError` is RAISED (NOT deterministic fallback) — preserves the auth-short-circuit invariant
- [ ] On all-candidate timeout (NOT auth) → deterministic fallback returned
- [ ] Escalation cascade: normal → aggressive → deterministic, with "didn't compress" trigger at each step
- [ ] Hard cap (`summary_max_overage_factor`): output >3× target → call `_cap_summary_text`, level becomes `"capped"`
- [ ] `_resolve_target_tokens` matches TS formula exactly (192/96/512 floors, 0.35/0.20 ratios)
- [ ] `_with_timeout` uses `ThreadPoolExecutor` per ADR-017 (NOT `asyncio.wait_for`)
- [ ] `_normalize_completion_summary` drops reasoning/thinking blocks
- [ ] `_normalize_completion_summary` dedupes exact fragments preserving FIRST-SEEN order (NOT last-seen)
- [ ] Envelope-aware extraction triggers when `content` extraction is empty
- [ ] Reasoning-retry: pass `extra_body={"reasoning_effort": "low"}` if Hermes LLM client supports it, else document degradation
- [ ] Auth-short-circuit Wave inline comment present
- [ ] All TS unit tests in `test/summarize.test.ts` + `test/lcm-summarizer-reasoning.test.ts` ported
- [ ] PR description cites LCM commit SHA `1f07fbd`

## Tests

Port from `test/summarize.test.ts`, `test/lcm-summarizer-reasoning.test.ts`:

### Auth detection

- `_extract_provider_auth_failure require_structural_signal=True only triggers on 401 or explicit error.kind`
- `_extract_provider_auth_failure require_structural_signal=False triggers on text patterns ("unauthorized", "invalid token")`
- `_extract_provider_auth_failure does NOT trigger on plain text matches when structural signal required`

### Retry / fallback

- `summarize falls through to next candidate on first auth failure`
- `summarize raises LcmProviderAuthError when ALL candidates auth-fail` (does NOT return deterministic)
- `summarize returns deterministic fallback when ALL candidates time out`
- `summarize retries with skip_model_auth on first 401`
- `summarize skips skip_model_auth retry for runtime-managed providers`
- `summarize exponential backoff between candidates` (mock clock; assert backoff = 500/1000/2000/4000/8000 ms)
- `summarize backoff capped at 8000ms` (5th+ candidate uses 8000, not 16000)

### Normalization

- `_normalize_completion_summary drops reasoning blocks`
- `_normalize_completion_summary dedupes exact fragments first-seen wins`
- `_normalize_completion_summary envelope-aware extraction` (response wrapped in `{result: {content: [...]}}`)
- `_extract_incomplete_response_signals` triggers reasoning-low retry

### Escalation

- `_summarize_with_escalation falls to aggressive when normal didn't compress`
- `_summarize_with_escalation falls to deterministic when aggressive also didn't compress`
- `_summarize_with_escalation hard cap triggers when output > 3× target` (level=`"capped"`)
- `_summarize_with_escalation target token resolution` (4 cases: condensed-min-512, leaf-normal-floor-192, leaf-aggressive-floor-96, leaf-aggressive-cap)

### Timeout

- `_with_timeout raises SummarizerTimeoutError on timeout`
- `_with_timeout uses ThreadPoolExecutor` (assert no asyncio.wait_for in module body via `grep` smoke test)

## Estimated effort
12–16 hours (the longest issue in this epic — the fallback chain is dense)

## Confidence
85% — the algorithm is well-documented but the Hermes LLM-client shim is the new code. Reasoning-parameter and runtime-managed-auth detection are the two areas most likely to need post-port adjustment.
