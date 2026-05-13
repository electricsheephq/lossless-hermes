---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-06] tools: port lcm_synthesize_around (1477 LOC) + period parser'
labels: 'port, tool'
---

## Source (TypeScript)
- File: `src/tools/lcm-synthesize-around-tool.ts`
- Lines: 1–1477
- Function(s)/class(es): `createLcmSynthesizeAroundTool` factory, schema (lines 61–144), three window kinds (`time`, `semantic`, `period`), `parsePeriodShortcut` (lines 279–433 — timezone-aware), `selectTimeWindowLeaves`, `buildSourceText`, cache lookup, `dispatchSynthesis` call, session_key fallback chain.

## Target (Python)
- File: `src/lossless_hermes/tools/synthesize_around.py` + a **standalone unit** for the period parser at `src/lossless_hermes/tools/_period_parser.py` (the parser is exported separately and exercised by `v41-period-timezone.test.ts` in TS).
- Estimated LOC: ~1100 LOC (Python denser; main file ~900, parser ~200).

## Dependencies
- Depends on: #06-02, #06-03, #06-04, #06-05, Epic 01 storage (`lcm_synthesis_cache` + UNIQUE index per [Wave-10 in ADR-029](../../docs/adr/029-wave-fix-provenance.md)), `src/lossless_hermes/synthesis/dispatch.py` (the synthesis dispatcher port — Epic 07 deliverable).
- **Wave B dependency:** semantic window kind requires Epic 05 (Voyage + vec0). Ship time + period in Wave A; semantic in Wave B.

## Acceptance criteria
- [ ] `LCM_SYNTHESIZE_AROUND_SCHEMA` dict — **description string verbatim** from `lcm-synthesize-around-tool.ts:641–653` (tools.md lines 331–333). The Wave-12 fix note about "the audit table records the resolved model that actually ran" is load-bearing prose — keep verbatim.
- [ ] **Validate `window_kind`** — must be `time` | `semantic` | `period`. Target required for time/semantic; optional for period.
- [ ] **Numeric clamps:** `windowHours ∈ [1, 672 (4 weeks)]`, `windowK ∈ [1, 200]`.
- [ ] **Resolve `tier`** → `'custom'` or `'filtered'`.
- [ ] **Parse `since` / `before`** as ISO timestamps; combine with window bounds.
- [ ] **Resolve conversation scope** (#06-05).
- [ ] **Resolve `session_key_for_cache`** — non-trivial fallback chain: `targetSummary?.session_key → input.sessionKey → conversation.session_key → 'agent:main:main'`. Prevents cross-session cache pollution. Pin against the TS source exactly.
- [ ] **Period parser** (`_period_parser.py`):
  - Self-contained, no DB deps, no I/O.
  - Accepts: `today | yesterday | this-week | last-week | this-month | last-month | last-7-days | last-30-days | last-Nh | last-Nd` (case-insensitive).
  - `get_local_day_start_utc(tz: ZoneInfo, dt: datetime)` — iterative-converge handles half-hour offsets (Asia/Kolkata) + DST transitions.
  - `get_local_day_duration_ms(tz: ZoneInfo, dt: datetime)` — handles 23h/25h DST days.
  - Uses Python `zoneinfo` (stdlib 3.9+); no third-party tz library.
  - **Exported separately** for testing — same shape as TS where it's exposed for `v41-period-timezone.test.ts`.
- [ ] **Pick leaves:**
  - `period`: `select_time_window_leaves(db, {range_start, range_end, scope})` — pure SQL, `kind='leaf' AND suppressed_at IS NULL AND julianday(COALESCE(latest_at, created_at)) BETWEEN ? AND ?`.
  - `time`: anchor on `targetSummary.created_at`, ±`windowHours`, then `select_time_window_leaves`.
  - `semantic`: `run_semantic_search(db, voyage, {queryText, topK: windowK, kind: 'leaf', conversationIds})`. **Requires Voyage + vec0 — Wave B.**
- [ ] **Build source text** (`build_source_text`): concatenate leaves with `### Leaf <id> (<ts>)` separator, hard-cap at `MAX_SOURCE_TEXT_TOKENS = 50_000`.
- [ ] **Cache lookup:** `SELECT * FROM lcm_synthesis_cache WHERE session_key=? AND range_start=? AND range_end=? AND leaf_fingerprint=? AND tier=? AND prompt_id=?` (single-flight via `INSERT OR IGNORE` on the UNIQUE index). Per **Wave-10** ([ADR-029](../../docs/adr/029-wave-fix-provenance.md)), the UNIQUE index includes `tier_label + prompt_id`. Inline comment at the cache write:
  ```python
  # LCM Wave-10 (2026-03-22): cache UNIQUE index includes tier_label + prompt_id
  # to prevent cross-tier cache collisions.
  # Original: lossless-claw/src/synthesis/dispatch.ts (and src/db/migration.ts).
  ```
- [ ] **If cache miss → call `dispatch_synthesis`** with tier + prompt + sourceText. Per **Wave-12 W2A1 fix** (tools.md line 391), this tool IS now wrapped in `runWithTokenGate` (was previously skipped). Estimator: flat `6_000` tokens.
- [ ] **Persist to `lcm_synthesis_cache`** (single-flight `INSERT OR IGNORE`).
- [ ] **Return markdown** with telemetry: `window`, `tier`, `cacheHit`, `model`, `latencyMs`, `voyageTokensConsumed` (semantic only), `leafIds[]`, `totalSourceTokens`, `truncatedAt`.
- [ ] **Failure modes** per tools.md "Failure modes" subsection:
  - Invalid `window_kind` → structured error.
  - Missing target for time/semantic → structured error.
  - Invalid ISO timestamp → structured error.
  - since >= before → structured error.
  - target sum_xxx not in scope → structured error.
  - semantic mode with `VOYAGE_API_KEY` missing → `VoyageError(kind='auth')` → structured error suggesting fallback.
  - vec0 unavailable → `SemanticSearchUnavailableError` → structured error.
  - Synthesis dispatch fails (auth, quota, content-filter) → `SynthesisDispatchError` → structured error w/ retry hint.
  - Period parser unrecognized → structured error w/ examples.
- [ ] PR description cites the LCM commit SHA being ported.

## Tests
- Mirror `lcm-synthesize-around-tool.test.ts` 1:1 in `tests/tools/test_lcm_synthesize_around.py` (~757 TS LOC → ~600 pytest LOC).
- **Period parser standalone tests** in `tests/tools/test_period_parser.py`:
  - Mirror `v41-period-timezone.test.ts` 1:1 — every shortcut × multiple timezones (UTC, America/Los_Angeles, Asia/Kolkata, Europe/London during DST transition).
  - Half-hour offset edge case (Asia/Kolkata at midnight crossing).
  - DST spring-forward (23h day) + fall-back (25h day).
  - `last-Nh` / `last-Nd` parametric forms.
- **Main tool tests:**
  - All three window kinds happy path.
  - Cache hit (second identical call returns cached row, `cacheHit=True`).
  - Cache miss → calls dispatch, persists, returns.
  - `session_key_for_cache` fallback chain (4 levels).
  - Semantic with vec0 missing → graceful error.
  - **Wave-10 regression:** seed two cache rows with same range but different `tier_label`; assert no collision.
  - **Wave-12 W2A1 regression:** when token budget would refuse, the gate fires (since this tool was previously not gated).

## Estimated effort
**16 hours** — 4h period parser (standalone, well-spec'd), 6h main tool port, 6h tests (the timezone matrix is the heaviest).

## Confidence
**85%** — period parser is 95% confident (zoneinfo handles everything); main tool 85% (depends on `dispatch_synthesis` being ready in Epic 07, which the tool calls into). 5% risk on the semantic path if Epic 05 surfaces vec0 issues; 5% on the cache UNIQUE-index migration being shipped by Epic 01.

## References
- [`docs/porting-guides/tools.md`](../../docs/porting-guides/tools.md) "lcm_synthesize_around" section (lines 325–392).
- [ADR-029](../../docs/adr/029-wave-fix-provenance.md) — Wave-10 (cache UNIQUE index), Wave-12 W2A1 (token gate wrap), inline-comment format.
- TS test fixtures: `test/lcm-synthesize-around-tool.test.ts` (757 LOC) + `test/v41-period-timezone.test.ts` (period parser).
