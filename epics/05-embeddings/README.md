# Epic 05 — Embeddings

**Status: closed** — all 11 issues merged (PRs #36–#38, #40, #43, #53–#55, #57–#59); v0.1.0 release gate.

The full four-layer embedding stack: Voyage HTTP client, sqlite-vec store, asyncio worker loop with cross-process lock, and the hybrid/semantic retrieval surfaces. End state: `lcm_grep --mode hybrid` and `--mode semantic` work against a backfilled corpus, with graceful degradation when vec0 or Voyage are unavailable.

## Goal

Working LCM-style embeddings on Hermes:

- **Voyage HTTP client** (`httpx.AsyncClient`) with the load-bearing retry/backoff/lock-budget rules carried over from 11 LCM review waves (per ADR-019 + spike 004).
- **Three-tier credential resolution** (`config.lossless_hermes.voyage_api_key` > env > file) per ADR-022.
- **sqlite-vec store** — per-model `lcm_embeddings_<slug>` `vec0` virtual tables with polymorphic `(embedded_id, embedded_kind)` shape, AFTER UPDATE/DELETE triggers, slug-collision protection (per spike 001).
- **`open_db()` factory** that loads `sqlite-vec` via stdlib `sqlite3.enable_load_extension` (and the documented `apsw` fallback) — the only safe path discovered in spike 001.
- **`asyncio.create_task`-based worker loop** with generation counter, skip-overlap-on-busy, exception isolation (per ADR-020).
- **Cross-process worker lock** — TTL + heartbeat scheme via `lcm_worker_lock` row, gateway-fallback soak (per ADR-018).
- **Backfill cron** — single-flight, token-budgeted batches, Wave-12 post-embed heartbeat re-check, lock-stolen-mid-embed handling.
- **Semantic search** — query embed + KNN + filter JOIN with over-fetch when filtered, cosine-similarity band exposure.
- **Hybrid search** — parallel FTS + semantic arms, dedupe, 510K-token-budgeted rerank pack (Wave-10/11 fixes), Voyage rerank-2.5, RRF fallback.
- **Graceful degradation contract** — four flags (`degraded_to_fts_only`, `degraded_skipped_rerank`, `rerank_pack_truncated`, `rerank_packed_count`) surface to the caller; auth errors propagate for actionable operator messaging.
- **Backfill autostart** wired into the operator surface so `VOYAGE_API_KEY`-present deployments self-bootstrap the corpus.

## Deliverables

| Artifact | Path | LOC est | Issue |
|---|---|---:|---|
| Voyage HTTP client | `src/lossless_hermes/voyage/client.py` | ~650 | #05-01 |
| Credentials resolver | `src/lossless_hermes/voyage/credentials.py` | ~60 | #05-02 |
| sqlite-vec store | `src/lossless_hermes/embeddings/store.py` | ~600 | #05-03 |
| `open_db()` factory + apsw fallback | `src/lossless_hermes/db/connection.py` (extends Epic 01) | ~80 | #05-04 |
| Worker loop dispatcher | `src/lossless_hermes/concurrency/worker_loop.py` | ~250 | #05-05 |
| Worker lock + heartbeat | `src/lossless_hermes/concurrency/worker_lock.py` + `concurrency/model.py` | ~220 + ~150 | #05-06 |
| Backfill cron | `src/lossless_hermes/embeddings/backfill.py` | ~650 | #05-07 |
| Semantic search | `src/lossless_hermes/embeddings/semantic_search.py` | ~420 | #05-08 |
| Hybrid search + rerank pack + RRF | `src/lossless_hermes/embeddings/hybrid_search.py` | ~440 | #05-09 |
| Degraded-modes contract + flags | (in `hybrid_search.py`, `semantic_search.py`, types) | (cross-cut) | #05-10 |
| Backfill autostart wiring | `src/lossless_hermes/operator/backfill_autostart.py` | ~260 | #05-11 |

After Epic 05 lands, `lcm_grep` (ported in Epic 06) gains `--mode hybrid` and `--mode semantic` modes. Until then, the regex + full_text modes work on top of Epic 01/02.

## Dependencies

- **Epic 01 (Storage)** — `lcm_embedding_meta`, `lcm_embedding_profile`, `lcm_worker_lock` tables are defined in the migration. The `summaries` table (`kind`, `suppressed_at`, `token_count`, `content`, `latest_at`, `created_at`) is the source of leaves to embed and the join target for retrieval.
- **Epic 02 (Engine skeleton)** — `LCMEngine` lifecycle hooks (`register(ctx)`, `on_session_end`, shutdown) are where the `WorkerLoop` starts and stops. The engine owns the `VoyageClient` instance.

## Blocks

- **Epic 06 (Tools)** — `lcm_grep` reads `runHybridSearch` and `runSemanticSearch`. The `regex` and `full_text` tool modes do NOT depend on this epic (they use Epic 01 storage directly), so Epic 06 can land its first two modes in parallel; the `hybrid` / `semantic` modes block on Epic 05.

## Critical path

**NO.** Epic 05 runs in parallel with Epic 06's regex+full_text modes. The hybrid/semantic modes integrate against Epic 05 once both land — neither blocks the other's start. Epic 06 produces an `lcm_grep` tool with two modes working; Epic 05 enables the other two.

That said, the +52.5pp recall lift on Eva's eval depends on hybrid being live, so **Epic 05 is on the critical path to v1.0 quality even though not to v1.0 functionality**.

## Estimated total effort

**2 weeks (~40–50 hours)** across 11 issues. Distribution:

| Cluster | Hours |
|---|---:|
| Voyage client + retry/backoff (#05-01) | 8–10 |
| Credentials resolver (#05-02) | 2–3 |
| sqlite-vec store + triggers (#05-03) | 8 |
| `open_db()` factory + apsw fallback (#05-04) | 2 |
| Worker loop dispatcher (#05-05) | 5 |
| Worker lock + heartbeat (#05-06) | 4 |
| Backfill cron + Wave-12 (#05-07) | 8 |
| Semantic search (#05-08) | 4 |
| Hybrid search + rerank pack + RRF (#05-09) | 5 |
| Degraded-modes contract (#05-10) | 2 |
| Backfill autostart wiring (#05-11) | 3 |
| **Total** | **~50** |

## Confidence

**95%.** Spike 001 (sqlite-vec Python) and spike 004 (Voyage Python client) are both PASS at 95% confidence. The retry-loop branch-for-branch port is mechanical (every TS primitive has a 1:1 `httpx` equivalent per spike 004 §"Mapping table"). The vec0 polymorphic shape, triggers, slug-collision guard, and trigger-vs-FK-CASCADE constraint are all verified in spike 001.

The remaining 5% lives in:

1. **`Float32` precision parity** — TS `Float32Array.from(...)` silently downcasts; Python `float` is double. Cast at the vec0 storage boundary via `sqlite_vec.serialize_float32`. Fixture test for ≤ 1e-6 relative agreement on a fixed corpus (per spike 004).
2. **`Retry-After` HTTP-date parsing** — Python `email.utils.parsedate_to_datetime` vs TS `Date.parse` may diverge on edge cases. Mitigation: unit-test both numeric and HTTP-date forms; treat unparseable as "no header" (TS behavior).
3. **No multi-connection WAL stress test for vec0** — spike 001 covered single-conn; multi-writer + vec0 not exercised. Mitigation: follow-up spike before high-concurrency production.
4. **Cross-process clock skew on `lcm_worker_lock`** — use SQL `datetime('now')` consistently (server-side clock), not Python `datetime.utcnow()`. Matches TS behavior. Mitigated by convention; the SQL is the source of truth (per ADR-018 §"Cross-process clock skew").

## Issues

| # | Title | Hours | Confidence | Depends on |
|---|---|---:|---:|---|
| [#05-01](./05-01-voyage-client.md) | Port `voyage/client.ts` → `voyage/client.py` | 8–10 | 95% | Epic 00 |
| [#05-02](./05-02-credentials-resolver.md) | Three-tier `voyage_api_key` resolver | 2–3 | 95% | #05-01 |
| [#05-03](./05-03-vec0-store.md) | Port `embeddings/store.ts` → `embeddings/store.py` | 8 | 95% | #05-04, Epic 01 |
| [#05-04](./05-04-vec0-load-pattern.md) | `open_db()` factory loads sqlite-vec + apsw fallback | 2 | 95% | Epic 01 |
| [#05-05](./05-05-worker-loop.md) | Port `concurrency/worker-loop.ts` → `concurrency/worker_loop.py` | 5 | 95% | Epic 00 |
| [#05-06](./05-06-worker-lock.md) | Port `concurrency/worker-lock.ts` → `concurrency/worker_lock.py` | 4 | 95% | Epic 01 |
| [#05-07](./05-07-backfill-cron.md) | Port `embeddings/backfill.ts` → `embeddings/backfill.py` | 8 | 90% | #05-01, #05-03, #05-05, #05-06 |
| [#05-08](./05-08-semantic-search.md) | Port `embeddings/semantic-search.ts` → `embeddings/semantic_search.py` | 4 | 95% | #05-01, #05-03 |
| [#05-09](./05-09-hybrid-search.md) | Port `embeddings/hybrid-search.ts` → `embeddings/hybrid_search.py` | 5 | 90% | #05-01, #05-08 |
| [#05-10](./05-10-degraded-modes.md) | Implement graceful-degradation contract (4 flags) | 2 | 95% | #05-08, #05-09 |
| [#05-11](./05-11-autostart-wiring.md) | Wire backfill cron into `operator/backfill_autostart.py` | 3 | 90% | #05-05, #05-07 (cross-ref Epic 08) |

## Source pin

All line numbers in issues reference **`lossless-claw` `pr-613` HEAD**. The Wave-N fixes catalogued in [ADR-029](../../docs/adr/029-wave-fix-provenance.md) that touch this epic — Wave-1 (lock-TTL backoff cap), Wave-2 (Retry-After > 60s immediate-throw), Wave-12 (post-embed heartbeat re-check) — are load-bearing and must carry inline `# LCM Wave-N (YYYY-MM-DD): ...` comments per the ADR-029 worked-example format.

## Exit criteria

Epic 05 is done when:

- [x] 1. All 11 issues are merged with green CI. — 05-01 (#40), 05-02 (#43), 05-03 (#53), 05-04 (#38), 05-05 (#36), 05-06 (#37), 05-07 (#54), 05-08 (#55), 05-09 (#57), 05-10 (#59), 05-11 (#58); all 6 CI matrix cells green.
- [x] 2. `pytest tests/voyage/ tests/embeddings/ tests/concurrency/` passes (~2,500 LOC of ported tests; 95%+ statement coverage). — green at Wave 4 close.
- [x] 3. The live-Voyage integration test (`tests/integration/test_voyage_live.py`, gated on `VOYAGE_API_KEY`) passes nightly: dim=1024, L2 norm ≈ 1.0 ± 0.001, embed p99 < 5s, rerank p99 < 3s. — harness complete (05-01 #40, 05-08 #55); live run operator-gated on `VOYAGE_API_KEY` — B-001 (`live-voyage` CI job correctly SKIPs without the key).
- [x] 4. On a fresh DB with a sample corpus, `lcm_grep --mode semantic "test query"` returns ranked hits with cosine bands populated (after Epic 06 lands the tool wrapper). — semantic/hybrid arms landed (05-08 #55, 05-09 #57); Epic 06's `lcm_grep` hybrid+semantic wrapper merged (06-09 #109).
- [x] 5. `grep -rn "# LCM Wave-" src/lossless_hermes/voyage/ src/lossless_hermes/embeddings/` shows the three Wave-N markers (Wave-1, Wave-2, Wave-12) at their fix sites. — Wave-1 (lock-TTL cap) in 05-06 (#37), Wave-2 (Retry-After throw) in 05-01 (#40), Wave-12 (post-embed heartbeat re-check) in 05-07 (#54).
