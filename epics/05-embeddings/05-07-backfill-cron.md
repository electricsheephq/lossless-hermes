---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] embeddings: port backfill.ts → embeddings/backfill.py'
labels: 'port, embeddings, backfill, worker'
---

## Source (TypeScript)
- File: `lossless-claw/src/embeddings/backfill.ts`
- Lines: 637
- Function(s)/class(es): `tickEmbeddingBackfill` (top-level entrypoint, ~219-436), `selectNextBatch` (472-522 — candidate SELECT with `NOT EXISTS lcm_embedding_meta`), `packBatches` (531-546 — greedy bin-pack to 80K tokens/batch), `writeBatch` (per-row SAVEPOINT vec0 + meta atomic), `countPendingDocs` (439-468), `BackfillResult` + `BackfillSkippedDoc` dataclasses (180-195).

## Target (Python)
- File: `src/lossless_hermes/embeddings/backfill.py`
- Estimated LOC: ~650

## Dependencies
- Depends on: #05-01 (Voyage client), #05-03 (vec0 store — `record_embedding`, `is_embedded`, `ensure_embeddings_table`), #05-05 (worker loop — backfill runs as a tick), #05-06 (worker lock — single-flight cross-process).
- Blocks: #05-11 (autostart wiring); #05-08 + #05-09 transitively need a populated vec0 to return results.

## Acceptance criteria

- [ ] **`tick_embedding_backfill(db, *, model_name, voyage_model, voyage, input_type="document", ...) -> BackfillResult`** is the per-tick entrypoint. Called by the worker loop (#05-05) every `interval_s` (default 60s per ADR-020).
- [ ] **Per-tick limits** (defaults match TS):
  - `per_tick_limit: int = 200` (max docs processed per tick).
  - `min_token_count: int = 1` (skip empty stubs).
  - `max_token_count: int = MAX_TOKENS_PER_EMBED_DOC` (27_000).
  - `max_batch_tokens: int = MAX_TOKENS_PER_EMBED_BATCH` (80_000).
  - `voyage_max_retries: int = 1` (lower than client default 3 — caps worst-case batch wall-time below 90s lock TTL).
  - `voyage_timeout_ms: int = 30_000` (lower than client default 60s — same reason).
  - `max_requests_per_second: float = 0.5` (one Voyage request per 2s — generous margin under Voyage tier-1 5 RPS).
- [ ] **Candidate SELECT** (port of `backfill.ts:472-522`):
  ```sql
  SELECT s.summary_id, s.content, s.token_count
    FROM summaries s
    WHERE s.suppressed_at IS NULL
      AND s.token_count BETWEEN ? AND ?    -- min_token_count, max_token_count
      AND s.kind = 'leaf'
      AND s.summary_id NOT IN (...)        -- failed-this-tick blocklist (dynamic IN list)
      AND NOT EXISTS (
          SELECT 1 FROM lcm_embedding_meta m
            WHERE m.embedded_id = s.summary_id
              AND m.embedded_kind = ?
              AND m.embedding_model = ?
              AND m.archived = 0
      )
    ORDER BY s.summary_id DESC               -- newest-first (newer content queryable faster)
    LIMIT ?
  ```
  Failed-this-tick blocklist is rebuilt per-tick from in-memory state (no persistence — a doc that 400'd on tick N may be retried on tick N+1 if the leaf content changed).
- [ ] **`pack_batches(docs, max_batch_tokens)`** (port of `backfill.ts:531-546`): greedy bin-pack, NO re-sorting. `sum(token_count for d in batch) <= max_batch_tokens`. Over-cap docs (token_count > max_token_count) are filtered out BEFORE packing and recorded in `result.skipped_over_cap` (preserves the lossless contract — splitting changes the semantic unit).
- [ ] **Tick algorithm** (port of `backfill.ts:219-436`):
  ```
  1. Validate: vec0 loaded (try_load_sqlite_vec); embeddings table exists for model
  2. Acquire worker lock (kind="embedding-backfill", worker_id=generate_worker_id("backfill"))
     Skip-lock flag (`skip_lock=True` for tests) bypasses. If not acquired → return BackfillResult(lock_not_acquired=True)
  3. Try/finally to ensure lock release (even on auth re-throw)
  4. While processed < per_tick_limit:
     a. SELECT next batch (cap 64 per SELECT — TS magic number)
     b. Partition: over-cap docs → result.skipped_over_cap; queryable → pack_batches
     c. For each batch:
        - Rate-limit pacing: await asyncio.sleep(1 / max_requests_per_second)
        - heartbeat_lock; if False → mark lock_stolen_mid_tick; break
        - voyage.embed(texts, model=voyage_model, input_type=input_type, output_dimension=profile.dim)
          (OUTSIDE any DB transaction — §0 invariant)
          * VoyageError(kind="auth") → re-throw (fatal; the lock will release via finally)
          * Other VoyageError → record per-doc skipped (skipped_reason="voyage_4xx" or "voyage_5xx" or "voyage_network"); continue
        - WAVE-12 FIX: heartbeat_lock AGAIN post-embed
          * If False → abort writes for this batch, mark each doc as lock_stolen_mid_embed; continue
        - write_batch (per-row SAVEPOINT; vec0 + meta atomic per row):
          BEGIN IMMEDIATE
          for each (doc, vec):
              SAVEPOINT sp_<random>
              record_embedding(...)  → on per-row error: ROLLBACK TO; continue
              RELEASE
          COMMIT (or ROLLBACK on tx-level error)
  5. Release lock in finally (always)
  6. Return BackfillResult(...)
  ```
- [ ] **Wave-12 fix (load-bearing):** the second `heartbeat_lock` call AFTER the Voyage embed call. Carry inline `# LCM Wave-12 (2026-04-XX): post-embed heartbeat re-check prevents a stale worker (heartbeat lapsed during the 60s Voyage call) from writing an embed for a row another worker now owns. Without this re-check: 60s Voyage timeout + 30s heartbeat interval = up to 90s of silence = lock TTL crossed. Original: lossless-claw/src/embeddings/backfill.ts:<line>.` per ADR-029.
- [ ] **§0 invariant:** the Voyage call MUST be OUTSIDE any DB transaction. The vec0+meta write happens in a separate `BEGIN IMMEDIATE` block AFTER the Voyage response is in hand. Document with an `assert_no_open_tx(conn)` runtime check before the Voyage call.
- [ ] **`BackfillResult` dataclass** (port of `backfill.ts:180-195`):
  ```python
  @dataclass
  class BackfillSkippedDoc:
      summary_id: str
      reason: Literal["voyage_400", "voyage_other", "lock_stolen_mid_embed", "over_cap"]
      detail: str | None = None

  @dataclass
  class BackfillResult:
      embedded_count: int            # vec0+meta inserts succeeded
      skipped_over_cap: int          # docs > max_token_count (no quota spent)
      skipped: list[BackfillSkippedDoc]
      per_tick_limit_reached: bool   # caller schedules next tick
      lock_not_acquired: bool        # caller skips this tick
      voyage_tokens_consumed: int    # from API usage.total_tokens
      duration_ms: int
  ```
- [ ] **`count_pending_docs(db, *, model_name, ...) -> int`** (port of `backfill.ts:439-468`): same SELECT shape with `COUNT(*)`. Used by `/lcm health` (Epic 08) to surface backlog.
- [ ] **Per-row SAVEPOINT with random suffix:** `f"sp_emb_{secrets.token_hex(8)}"`. Two concurrent writers (different processes) holding different locks for different job-kinds could theoretically race the same SAVEPOINT name; the random suffix prevents collision.
- [ ] **Rate-limit pacing** via `await asyncio.sleep(1.0 / max_requests_per_second)` between Voyage calls. Single-flight via the lock makes per-process RPS accurate.
- [ ] **Test coverage:** all 474 LOC of `test/embeddings-backfill.test.ts` ported to `tests/embeddings/test_backfill.py`.
- [ ] `mypy --strict` and `ty check` pass.

## Tests (`tests/embeddings/test_backfill.py`)

Cases from `test/embeddings-backfill.test.ts` (474 LOC):

- Embeds all pending leaves; result count matches; `is_embedded` true after.
- Skips suppressed leaves (no Voyage call — verify via mock call count).
- Skips already-embedded leaves on subsequent ticks (idempotent — second tick → `embedded_count=0, voyage_tokens_consumed=0`).
- Over-cap leaves (`token_count > max_token_count`) → `result.skipped_over_cap` populated; no Voyage quota spent.
- `per_tick_limit` caps work; `per_tick_limit_reached=True`.
- Voyage 400 records skipped doc with `reason="voyage_400"`; tick continues on remaining batches.
- Voyage 401 is fatal — `VoyageError(kind="auth")` re-thrown; lock released via finally (verify via post-test `lock_info` returning None).
- Voyage 500 on first batch — marks docs skipped with `reason="voyage_other"`; remaining batches still processed.
- Lock contention (a peer worker holds the lock) → `result.lock_not_acquired=True`; no Voyage calls.
- Releases lock on success.
- Releases lock on auth-re-throw.
- `pack_batches` respects `max_batch_tokens` (no batch exceeds the cap).
- `count_pending_docs` accurate before and after a tick.
- **Wave-12 fix:** mock the lock to be stolen between Voyage call and write; verify writes are aborted and docs marked `reason="lock_stolen_mid_embed"`.
- **§0 invariant:** runtime assertion fires if a write transaction is open when `voyage.embed` is called (use the `assert_no_open_tx` helper).
- Per-row SAVEPOINT: stage a single doc in a batch of 3 that fails `record_embedding` (e.g. dim mismatch); verify the other 2 land and the failed one rolls back without aborting the whole batch.

## Estimated effort
8 hours

## Confidence
90% — the algorithm is fully specified by the porting guide §"Backfill cron" (lines 1100-1180). The Wave-12 fix is the most subtle part — the porting guide warns that without it, "60s Retry-After + 30s timeout = 90s = TTL". Test coverage explicitly exercises the steal-mid-embed scenario. Residual 10%:
- The 64-per-SELECT magic number isn't deeply justified in the TS source. Use it verbatim; document the TS reference line.
- Rate-limit pacing under high lock-acquire-fail rates: if 9 of 10 workers can't acquire, the one that did paces at 0.5 RPS — fine. But if the lock is held by a peer process that died without releasing, the GC step in `acquire_lock` only fires at acquisition; a long-lived peer holding the lock blocks indefinitely. Mitigated by ADR-018 `GATEWAY_FALLBACK_SOAK_MS = 300_000`.

## Files to read before starting
- `docs/porting-guides/embeddings.md` §"Backfill cron" (lines 1099-1180)
- `docs/adr/018-concurrency-model.md` §"§0 invariant"
- `docs/adr/029-wave-fix-provenance.md` (Wave-12 row + inline comment format)
- TS source: `lossless-claw/src/embeddings/backfill.ts` (entire — 637 LOC)
- TS tests: `lossless-claw/test/embeddings-backfill.test.ts` (entire — 474 LOC)
