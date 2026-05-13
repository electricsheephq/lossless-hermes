# ADR-029: Wave-N fix provenance

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

LCM v4.1 carries scar tissue from 12+ audit waves (the "Wave-N" series) that landed real production fixes for race conditions, retry storms, prompt injections, and concurrency invariants. These fixes are NOT obvious from reading the code — many look like trivial one-line changes (`INSERT OR IGNORE` instead of `INSERT`, a 25s constant in a backoff helper, a `tier_label + prompt_id` clause in a UNIQUE index) but each one prevents a specific previously-observed bug.

A 1:1 source-to-Python port risks silently regressing these fixes because:

- A reviewer unfamiliar with the audit history sees an `INSERT OR IGNORE` and thinks "why not a regular INSERT?" — and may "clean up" the code, reintroducing a race.
- A future refactor (e.g. swapping `INSERT OR IGNORE` for `INSERT ... ON CONFLICT DO NOTHING` because it's "more idiomatic") might preserve semantics in the simple case but break a savepoint-scope assumption.
- The Wave-N fixes are distributed across multiple subsystems (extraction, voyage, synthesis, tools, backfill) — there is no single doc that lists them all in the LCM codebase.

The constraint forcing a choice: how do we make the scar-tissue lineage visible inline so it survives future refactors?

## Options considered

### Option A: Inline `# LCM Wave-N (date): description` comments at fix sites

- Description: At every Wave-N-load-bearing line or block in the Python port, add a comment in the format:
  ```python
  # LCM Wave-1 (2025-11-08): race-safe INSERT OR IGNORE prevents UNIQUE
  # constraint violations under concurrent entity-coreference ingest.
  # Original: lossless-claw/src/extraction/entity-coreference.ts:284.
  conn.execute("INSERT OR IGNORE INTO lcm_entities (...) VALUES (...)", ...)
  ```
- Pros:
  - **Inline visibility.** A reviewer looking at the line sees the rationale; they don't have to grep changelog.
  - **Refactor-resistant.** A future PR that touches the line forces the reviewer to confront the comment. Removing the comment is an explicit decision.
  - **Auditable.** A simple `grep -n "Wave-" src/lossless_hermes/` enumerates the scar tissue.
  - Costs ~2 lines per fix; the port has 8 known Wave-N fixes → 16 lines of comments.
- Cons:
  - Comments must be kept accurate. If a Wave-1 fix later becomes redundant (e.g. schema change makes the race impossible), the comment needs updating.

### Option B: Separate `WAVE_FIXES.md` doc + no inline comments

- Description: Maintain a markdown doc listing all Wave-N fixes with file:line references. No inline comments.
- Pros: Single source of truth; cleaner code.
- Cons:
  - **Bit-rot is silent.** When a future PR touches the Wave-1 line, the contributor has no signal that the line is load-bearing. The doc reference stays stale; the regression ships.
  - Doc-vs-code drift requires periodic audit.

### Option C: Inline comments + central index doc

- Description: Both A and B. Inline comments at fix sites; a `docs/wave-fix-index.md` enumerates all sites with permalinks.
- Pros: Belt-and-suspenders.
- Cons:
  - Two places to keep in sync. If the index drifts, contributors get confused about which to trust.
  - The inline comment carries the load-bearing signal; the index is a convenience. Doubling the cost for the convenience is not worth it.

### Option D: Custom decorator / annotation

- Description: Decorator like `@wave_fix("Wave-1", "race-safe INSERT OR IGNORE")` on the function or method.
- Pros: Programmatically discoverable.
- Cons:
  - Many Wave-N fixes are sub-function (a single line inside a method). Decorators can't annotate a line.
  - Adds an abstraction layer for a documentation concern. Comments are simpler.

## Decision

Chosen: **Option A — inline `# LCM Wave-N (date): description` comments at every load-bearing fix site**.

Format:

```python
# LCM Wave-N (YYYY-MM-DD): one-sentence description of what the fix prevents
# Original: lossless-claw/src/<path>:<line>
<the code>
```

Date is the rough month/year the fix landed in LCM (precision is not load-bearing; the Wave number is). Original path/line points to the TS source so reviewers can read the original commit and adjacent context.

## Known Wave-N fixes to preserve

The following Wave-N fixes are load-bearing for the port. Every one must carry an inline comment in the Python source.

| Wave-N | Subsystem | Fix | TS file | Python target |
|---|---|---|---|---|
| **Wave-1** | extraction/coreference | Race-safe `INSERT OR IGNORE` prevents UNIQUE constraint violations under concurrent ingest. Without it, two workers seeing the same canonical entity name simultaneously both try to INSERT, one fails, the failure aborts the surrounding txn. | `src/extraction/entity-coreference.ts` | `src/lossless_hermes/extraction/coreference.py` |
| **Wave-1** | voyage | Lock-TTL backoff cap (25s). The retry-with-exponential-backoff loop is capped at 25s per attempt so a runaway 429-storm cannot starve other workers waiting on the same row-uniqueness lock. | `src/voyage/client.ts` | `src/lossless_hermes/voyage/client.py` |
| **Wave-2** | voyage | `Retry-After > 60s immediate-throw`. When Voyage returns a `Retry-After` header indicating > 60s, the client throws immediately rather than sleeping — surface the rate-limit upward so the worker yields its row-lock instead of holding it through a long sleep. | `src/voyage/client.ts` | `src/lossless_hermes/voyage/client.py` |
| **Wave-4** | extraction (prompts) | Prompt-injection defense. Extraction prompts include explicit user-input-boundary markers and instructions to ignore embedded "ignore previous instructions" attempts in conversation content. | `src/extraction/entity-extractor-llm.ts` | `src/lossless_hermes/extraction/llm_extractor.py` |
| **Wave-7** | extraction/coreference | Per-row savepoint. Each entity-coreference resolution attempt is wrapped in its own SAVEPOINT so a single row's failure (e.g. constraint violation) does not abort the surrounding batch transaction. | `src/extraction/entity-coreference.ts` | `src/lossless_hermes/extraction/coreference.py` |
| **Wave-10** | synthesis | Cache UNIQUE index includes `tier_label + prompt_id`. Without these columns in the UNIQUE constraint, cache collisions could occur across tiers (e.g. an episodic-leaf v1 result was incorrectly returned for an episodic-condensed v1 lookup). | `src/synthesis/dispatch.ts` (and `src/db/migration.ts` for the index DDL) | `src/lossless_hermes/synthesis/dispatch.py` + `src/lossless_hermes/db/migration.py` |
| **Wave-12** | embeddings/backfill | Post-embed heartbeat re-check. After a successful Voyage embed call, the worker re-checks its heartbeat row before INSERTing the result. Without this re-check, a stale worker whose heartbeat lapsed could write an embed for a row another worker now owns. | `src/embeddings/backfill.ts` | `src/lossless_hermes/embeddings/backfill.py` |
| **Wave-12 F5** | tools | `runWithTokenGate` is middleware-not-decorator. Wrapping a tool handler in `runWithTokenGate` as middleware (not as a Python decorator) ensures the gate state is computed at invocation time, not at registration time. Decorator-time computation would freeze the gate state to whatever was true at plugin-init, defeating the purpose. | `src/plugin/needs-compact-gate.ts` + tool registration sites | `src/lossless_hermes/plugin/needs_compact_gate.py` + `src/lossless_hermes/tools/*` registration sites |

## Worked example

`src/lossless_hermes/extraction/coreference.py` (illustrative — placement inside the worker loop):

```python
async def _ingest_entity_mention(
    conn: sqlite3.Connection,
    entity_canonical: str,
    mention_text: str,
    message_id: int,
) -> None:
    # LCM Wave-7 (2026-02-14): per-row savepoint isolates a single row's failure
    # so the surrounding batch transaction survives constraint violations.
    # Original: lossless-claw/src/extraction/entity-coreference.ts:312.
    sp_name = f"sp_mention_{message_id}"
    conn.execute(f"SAVEPOINT {sp_name}")
    try:
        # LCM Wave-1 (2025-11-08): race-safe INSERT OR IGNORE prevents UNIQUE
        # constraint violations under concurrent entity-coreference ingest.
        # Without it, two workers seeing the same canonical name simultaneously
        # both try to INSERT, one fails, the failure aborts the surrounding txn.
        # Original: lossless-claw/src/extraction/entity-coreference.ts:284.
        conn.execute(
            "INSERT OR IGNORE INTO lcm_entities (canonical_name, ...) VALUES (?, ...)",
            (entity_canonical, ...),
        )
        conn.execute(
            "INSERT INTO lcm_entity_mentions (entity_id, message_id, ...) VALUES (?, ?, ...)",
            (...),
        )
        conn.execute(f"RELEASE SAVEPOINT {sp_name}")
    except sqlite3.IntegrityError:
        conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
        conn.execute(f"RELEASE SAVEPOINT {sp_name}")
        raise
```

## Rationale

These are scar-tissue fixes from real production incidents. Each one was added in response to an observed failure mode — not as a defensive precaution. Their value comes from the historical context, which is invisible from reading the code alone.

Option A (inline comments) is the lowest-overhead way to make the lineage refactor-resistant. A `grep -n "Wave-"` enumeration provides the audit trail. Future contributors who consider "cleaning up" a Wave-N-marked line are confronted with the rationale and forced to make an explicit choice.

Option B (separate doc) was rejected because the doc-vs-code drift is silent. The whole point of the comment is to catch the contributor's eye AT THE LINE. A separate doc requires the contributor to know it exists and check it — exactly the failure mode we are mitigating.

Option C (both) doubles the maintenance cost for a convenience benefit. If we ever build a Wave-N index doc, it should be auto-generated from the inline comments (a one-line `grep -rn "Wave-" src/lossless_hermes/` produces it).

Option D (decorator) cannot annotate sub-function lines and adds an abstraction layer. Comments are the right granularity.

## Consequences

- **Every Wave-N-load-bearing line carries an inline comment** in the exact format above. Reviewers in PR see the rationale.
- **`grep -rn "# LCM Wave-" src/lossless_hermes/`** enumerates the scar tissue at any time. This is the audit trail.
- **Adding a new Wave-N fix** during the port (or in a future patch) requires:
  1. Add the inline comment in the agreed format.
  2. Append a row to the table in this ADR (or supersede this ADR with a follow-up).
  3. Add a test that fails without the fix and passes with it (the regression test).
- **Removing a Wave-N comment** is an explicit decision recorded in the PR description. Mass-removal (e.g. "all Wave-1 fixes are now schema-impossible") triggers an ADR superseding this one with the removal rationale.
- **PR template** (`.github/PULL_REQUEST_TEMPLATE.md`) should include a checkbox: "Does this PR touch a `# LCM Wave-` line? If yes, justify the change in the description."
- **Test coverage:** every Wave-N fix has a corresponding regression test under `tests/`. The test name references the Wave number (e.g. `test_wave1_race_safe_insert_or_ignore_under_concurrent_ingest`).
- **Invariant:** the comment block lives immediately above (or on the same line as) the code it explains. Comments separated from their code by blank lines or unrelated statements are out of scope.
- **Invariant:** the comment cites the original TS file:line so historians can read the original commit message.
- **No central wave-fix index doc.** If one is needed, generate from `grep` output. The inline comments are the source of truth.

## Open questions / 5% uncertainty

1. **Additional Wave-N fixes discovered during the port.** The 8 listed above are the known load-bearing ones, but the port may surface more (e.g. a Wave-5 fix in a file we haven't read yet). Action: as new Wave-N markers are found in TS, add them to this ADR's table or supersede this ADR.
2. **Wave-N fix that becomes redundant due to a schema change.** Example: if a future migration adds a UNIQUE constraint that subsumes the application-level `INSERT OR IGNORE` guard, the inline comment is technically stale. Policy: keep the comment AND the code together for clarity; the comment still documents WHY the code exists, even if the schema also enforces it. A future ADR may justify removing both together.
3. **Aggregated Wave-N comments** (e.g. one block of code addresses Wave-1, Wave-7, and Wave-10 simultaneously). Format as a list:
   ```python
   # LCM Wave-1 (2025-11-08): race-safe INSERT OR IGNORE.
   # LCM Wave-7 (2026-02-14): per-row savepoint isolates failures.
   # LCM Wave-10 (2026-03-22): tier_label + prompt_id in UNIQUE index.
   # Original: lossless-claw/src/<paths>.
   ```
4. **Date precision.** Wave numbers are stable across LCM history; exact dates are best-effort. If a date is wrong by a month, the Wave number still anchors the fix. Don't block PRs on date precision.
