---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] embeddings: port store.ts → embeddings/store.py'
labels: 'port, embeddings, vec0'
---

## Source (TypeScript)
- File: `lossless-claw/src/embeddings/store.ts`
- Lines: 609 (full file)
- Function(s)/class(es): `embeddingsTableName` (58-72), `candidateVec0Paths` (83-100 — drop in Python per spike 001), `tryLoadSqliteVec` (122-146 — collapses to ~10 LOC in Python), `vec0Version` (152-159), `ensureEmbeddingsTable` (206-253 — creates virtual table + AFTER UPDATE/DELETE triggers), `dropEmbeddingsTriggers` (263-275), `registerEmbeddingProfile` (294-351 — slug-collision check), `recordEmbedding` (366-443 — SAVEPOINT-per-row + DELETE-before-INSERT Wave-4 dup guard), `replaceEmbedding` (450-459), `deleteEmbedding` (465-477), `markEmbeddingSuppressed` (487-501 — UPDATE on metadata col only — vec0 v4.1.1 partition-key UPDATE corruption guard), `searchSimilar` (542-580 — `MATCH + k + metadata filter`), `embeddingsTableExists` (586-592), `isEmbedded` (598-609).

## Target (Python)
- File: `src/lossless_hermes/embeddings/store.py`
- Estimated LOC: ~600 (the `candidateVec0Paths` drop saves ~30 LOC; dataclass overhead adds ~20 LOC → net same)

## Dependencies
- Depends on: #05-04 (`open_db()` factory loads `sqlite-vec`); Epic 01 (`lcm_embedding_meta`, `lcm_embedding_profile`, `summaries` tables — schema must be in the migration).
- Blocks: #05-07 (backfill), #05-08 (semantic search; calls `searchSimilar`), #05-09 (hybrid search transitively).

## Acceptance criteria

- [ ] **Per-model virtual table shape** (`ensure_embeddings_table`, port of `store.ts:206-253`) creates:
  ```sql
  CREATE VIRTUAL TABLE IF NOT EXISTS lcm_embeddings_<slug> USING vec0(
      embedding float[<DIM>],
      +embedded_id text,      -- AUXILIARY: stored uncompressed, NOT filterable in MATCH
      embedded_kind text,     -- METADATA: filterable in MATCH (summary/entity/theme)
      suppressed integer      -- METADATA: filterable (0/1)
  );
  ```
  Column class choice is load-bearing per `store.ts:172-180` comments — `+` prefix on `embedded_id` is the auxiliary marker; `embedded_kind` and `suppressed` are metadata (filterable inside MATCH WHERE).
- [ ] **AFTER UPDATE trigger** (per-model, `store.ts:231-243`): `lcm_embed_suppress_<slug>` fires on `UPDATE OF suppressed_at ON summaries` when the NULL-ness flips; updates `lcm_embeddings_<slug>.suppressed`. Verbatim SQL from the porting guide.
- [ ] **AFTER DELETE trigger** (per-model, `store.ts:245-252`): `lcm_embed_delete_<slug>` fires on `DELETE ON summaries`; cascades DELETE in `lcm_embeddings_<slug>`. Per-model because vec0 SQL doesn't support dynamic table-name resolution inside triggers. **Triggers (not FK CASCADE)** because vec0 corrupts under foreign-key constraints (v4.1.1 finding documented in porting guide §"AFTER UPDATE + AFTER DELETE triggers").
- [ ] **Polymorphic `embedded_kind`** values are `"summary"`, `"entity"`, `"theme"`. The store accepts any of the three; the suppression trigger above only fires for `embedded_kind = 'summary'` (entities and themes have their own suppression paths handled in later epics).
- [ ] **Slug normalization** (`embeddings_table_name`, port of `store.ts:58-72`):
  - `MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")`. Reject if not matched.
  - `slug = re.sub(r"[^a-z0-9]", "", model_name.lower())`. Reject empty slug.
  - Return `f"lcm_embeddings_{slug}"`.
  - `voyage-4-large` → `lcm_embeddings_voyage4large`.
- [ ] **Slug-collision guard** (`register_embedding_profile`, port of `store.ts:294-351`): if a different `model_name` already exists with the same slug, raise. Defense in depth: the `lcm_embedding_profile.model_name` is PK so the canonical name can't collide, but two distinct canonical names that sluggify to the same value would target the same vec0 table — explicit reject.
- [ ] **Dim immutability:** `register_embedding_profile` is idempotent on `(model_name, dim)` match; raises on dim-mismatch for an existing profile (per `store.ts:308-320`).
- [ ] **`record_embedding`** (port of `store.ts:366-443`):
  - **DELETE-before-INSERT** in a transaction (Wave-4 duplicate guard — vec0 will happily store the same `(embedded_id, embedded_kind)` twice without this).
  - **SAVEPOINT-per-row with a crypto-random suffix** (`f"sp_emb_{secrets.token_hex(8)}"`) so two concurrent writers don't collide on savepoint names.
  - Inserts into both `lcm_embeddings_<slug>` AND `lcm_embedding_meta` atomically (one transaction).
  - Rejects wrong dim (compares against the registered profile's dim).
  - Requires the model profile to be registered first; raises if not.
- [ ] **Python sqlite3 integer simplification:** the TS `node:sqlite` BigInt dance (`store.ts:399: const suppressedBig = suppressed ? 1n : 0n;`) is unnecessary in Python — pass `1` or `0` directly. Document this in a code comment so future contributors don't reintroduce the dance.
- [ ] **`mark_embedding_suppressed`** (port of `store.ts:487-501`): `UPDATE lcm_embeddings_<slug> SET suppressed = ? WHERE embedded_id = ? AND embedded_kind = ?`. **Only updates the metadata column `suppressed`**, never the partition-key column `embedding` or the auxiliary `embedded_id`. vec0 v4.1.1 corrupts under partition-key UPDATEs — `replace_embedding` (DELETE + recordEmbedding) is the path for those.
- [ ] **`search_similar`** (port of `store.ts:542-580`):
  - `SELECT embedded_id, embedded_kind, distance FROM lcm_embeddings_<slug> WHERE embedding MATCH ? AND k = ? AND suppressed = 0 AND embedded_kind IN (...) ORDER BY distance`.
  - Vector bound as bytes via `sqlite_vec.serialize_float32(vector)` (preferred — 2.3× faster than JSON per spike 001) OR as JSON string via `json.dumps(list(vector))`. Both accepted by vec0 MATCH; bytes is the default. The caller passes `query_vector: list[float] | bytes`; the store accepts either.
  - Optional `exclude_suppressed: bool = True` (defense-in-depth — even though the trigger keeps `suppressed` in sync, a race window exists between summary suppression and trigger fire).
  - Optional `embedded_kind: list[str] | None` filter inside MATCH.
- [ ] **`try_load_sqlite_vec(conn, *, silent=False) -> bool`** (collapsed from `store.ts:122-146`):
  ```python
  def try_load_sqlite_vec(conn, *, silent=False) -> bool:
      try:
          conn.enable_load_extension(True)
          sqlite_vec.load(conn)
          conn.enable_load_extension(False)
          return True
      except (AttributeError, sqlite3.OperationalError) as e:
          if not silent:
              logger.warning(f"[embeddings.store] failed to load sqlite-vec: {e}")
          return False
      ```
  The TS `candidateVec0Paths` (lines 83-100) is dropped — `sqlite_vec.load(conn)` finds its own bundled extension via the PyPI package.
- [ ] **`vec0_version(conn) -> str | None`** (port of `store.ts:152-159`): `SELECT vec_version()`. Returns `None` on failure (extension not loaded).
- [ ] **`is_embedded(conn, embedded_id, embedded_kind, model_name) -> bool`** (port of `store.ts:598-609`): consults `lcm_embedding_meta` only (does NOT touch vec0). Used by `backfill` SELECT's `NOT EXISTS` pre-filter and by `/lcm health` for backlog counts.
- [ ] **`mypy --strict` and `ty check`** pass.
- [ ] **All 525 LOC of `test/embeddings-store.test.ts` ported** to `tests/embeddings/test_store.py`. The vec0-dependent block uses `@pytest.mark.skipif(not VEC0_AVAILABLE)` (port of TS `describe.skipIf`). `VEC0_AVAILABLE` is computed by attempting `try_load_sqlite_vec(in_memory_conn)` in conftest.

## Tests (`tests/embeddings/test_store.py`)

Cases from `test/embeddings-store.test.ts` (525 LOC):

**Non-vec0:**
- `embeddings_table_name` sluggification (canonical cases + reject cases for invalid model names).
- `vec0_version` returns None when extension not loaded.
- `try_load_sqlite_vec` returns False on missing extension.
- `register_embedding_profile`: insert idempotent on same dim; raises on mismatch; rejects bad name; rejects bad dim.

**Vec0-gated (`@skipif`):**
- Loads vec0; `vec0_version` returns a version string.
- `ensure_embeddings_table` creates virtual table; idempotent.
- `record_embedding` inserts into both vec0 + meta; `is_embedded` reflects.
- `record_embedding` rejects wrong dim.
- `record_embedding` requires registered profile (raises).
- `search_similar` finds nearest by L2; excludes suppressed by default.
- `search_similar` includes suppressed when `exclude_suppressed=False`.
- `search_similar` filters by `embedded_kind` list.
- `mark_embedding_suppressed` flips visibility in subsequent KNN.
- `replace_embedding` removes prior + inserts new (single transaction).
- `delete_embedding` removes from both vec0 and meta.
- Two different model profiles → two independent vec0 tables; data isolation.
- **AFTER UPDATE trigger:** `UPDATE summaries SET suppressed_at = '...' WHERE summary_id = ?` cascades to `lcm_embeddings_<slug>.suppressed=1`.
- **AFTER DELETE trigger:** `DELETE FROM summaries WHERE summary_id = ?` cascades to row removal in `lcm_embeddings_<slug>`.

## Estimated effort
8 hours

## Confidence
95% — spike 001 PASS validated the full vec0 polymorphic shape, the AFTER UPDATE on metadata col (safe), the slug pattern, and the trigger-vs-FK-CASCADE constraint. The TS SQL ports verbatim. Residual 5%:
- No multi-connection WAL stress test for vec0 (spike 001 single-conn). Triggers under concurrent writer + reader contention is documented as supported but not exercised. Mitigate with a follow-up spike if production traffic warrants.
- vec0 partition-key UPDATE corruption (v4.1.1) — we structurally avoid it by routing all id/kind changes through DELETE + INSERT; document the rule in a module-level docstring so contributors don't add a "convenience" UPDATE later.

## Files to read before starting
- `docs/porting-guides/embeddings.md` §"sqlite-vec store" (lines 413-560)
- `docs/spike-results/001-sqlite-vec-python.md` (entire — 92 LOC)
- `docs/adr/004-sqlite3-backend.md` (stdlib `sqlite3` primary; `apsw` opt-in fallback)
- TS source: `lossless-claw/src/embeddings/store.ts` (entire — 609 LOC)
- TS tests: `lossless-claw/test/embeddings-store.test.ts` (entire — 525 LOC)
