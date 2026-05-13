---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port semantic-infra-init.ts (one-time vec0 init)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/operator/semantic-infra-init.ts`
- Lines: 196 LOC
- Function(s)/class(es): `initSemanticInfraIfPossible(db, deps)`, internal `_loadVec0Extension`, `_registerEmbeddingProfile`, `_createVec0VirtualTable`, `_createSuppressionTriggers`

## Target (Python)

- File: `src/lossless_hermes/operator/semantic_infra.py`
- Estimated LOC: ~210

## What this issue covers

The one-time vec0 + embedding-profile bootstrap fired at plugin load (per plugin-glue.md "Plugin registration sequence" — implicit in the engine init). Idempotent: every call after the first is a no-op.

### Behavior

1. **Load the vec0 extension** — `sqlite_vec.load(conn)` per spike-001 / Epic 01-01's connection layer. The extension MUST already be loadable (set up in `db/connection.py`). If `sqlite-vec` is not installed, this issue's function returns `{"kind": "unavailable", "reason": "..."}` without raising.
2. **Register the embedding profile** — INSERT IGNORE into `lcm_embedding_profile` for the active model (`voyage-3-large` or operator-configured). Idempotent — multiple calls don't duplicate rows.
3. **Create the per-model vec0 virtual table** — `CREATE VIRTUAL TABLE IF NOT EXISTS lcm_embeddings_<slug> USING vec0(...)`. The `<slug>` is a sanitized version of the embedding model name.
4. **Create the suppression triggers** — `lcm_embed_suppress_<slug>` (AFTER UPDATE OF suppressed_at ON summaries) + `lcm_embed_delete_<slug>` (AFTER DELETE ON summaries). These triggers maintain the `suppressed` metadata column on the vec0 table so semantic search can filter at query time without joining (per doctor-ops.md §"Schema additions to support suppression" line 301).

### Return shape

```python
class SemanticInfraInitResult(BaseModel):
    kind: Literal["initialized", "already_initialized", "unavailable"]
    profile_id: str | None = None
    table_name: str | None = None
    triggers_created: list[str] = []
    reason: str | None = None
```

### Voyage-vs-other-embedder caveat

Per doctor-ops.md "Operator modules" line 313: "Yes if Hermes uses pgvector/Qdrant/other" — i.e., this module is DROPPED entirely if Hermes uses a different vector store. The v0.1 port assumes sqlite-vec (matching TS source and Epic 05's stance). If a future ADR-? swaps the vector store, this module is replaced wholesale, not refactored.

### Idempotency

The TS source is fully idempotent: `INSERT IGNORE`, `CREATE TABLE IF NOT EXISTS`, `CREATE TRIGGER IF NOT EXISTS`. The Python port keeps this contract. Running `init_semantic_infra_if_possible` twice in a row on a fresh DB:
- First call: `kind="initialized"`, three triggers created.
- Second call: `kind="already_initialized"`, zero triggers created (already present).

## Dependencies

- Depends on: #08-01 (dispatcher — although this is not directly user-facing as a `/lcm` subcommand, it runs at plugin init), Epic 01-01 (DB connection with vec0 loadable), Epic 01-06 (`lcm_embedding_profile` table schema + `lcm_embedding_meta` sidecar tables).
- Blocks: Epic 05 (embedding-store creation — `lcm_embeddings_<slug>` vec0 virtual table is created here, referenced by `embeddings/store.py`).

## Acceptance criteria

- [ ] `init_semantic_infra_if_possible(db, deps) -> SemanticInfraInitResult` returns the right `kind` based on state (initialized / already_initialized / unavailable).
- [ ] Idempotent: second call returns `kind="already_initialized"` with `triggers_created=[]`.
- [ ] sqlite-vec missing → `kind="unavailable"` with a clear reason; does NOT raise.
- [ ] Embedding profile row is INSERT-IGNORE'd; same model registered twice doesn't duplicate.
- [ ] `lcm_embeddings_<slug>` vec0 virtual table is created with the column shape from `embeddings/store.ts:ensureEmbeddingsTable` (1024-dim Voyage default; configurable via `deps.dim`).
- [ ] Both suppression triggers are created (`lcm_embed_suppress_<slug>` + `lcm_embed_delete_<slug>`).
- [ ] Trigger names embed the sanitized model slug (e.g. `voyage_3_large` for `voyage-3-large`).
- [ ] No dedicated TS test file (exercised via `test/v41-suppression-cascade-trigger.test.ts`).
- [ ] **New test:** `tests/operator/test_semantic_infra.py::test_idempotent_second_call` — initialize, then initialize again, assert no-op.
- [ ] **New test:** `tests/operator/test_semantic_infra.py::test_unavailable_when_vec_missing` — patch `sqlite_vec` to ImportError, assert `kind="unavailable"`.
- [ ] **New test:** `tests/operator/test_semantic_infra.py::test_triggers_fire_on_suppress` — initialize + UPDATE `summaries.suppressed_at`, assert vec0 metadata col flipped to 1.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Operator modules" line 313.
- [ ] `pytest tests/operator/test_semantic_infra.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/operator/semantic_infra.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**3 hours.**

## Confidence

**92%** — small file, well-understood vec0 init pattern; the only uncertainty is in the trigger-naming convention (slug sanitization), which is settled by the TS source (`embeddings/store.ts:slugForModel`).
