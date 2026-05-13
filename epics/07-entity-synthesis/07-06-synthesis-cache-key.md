---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-07] synthesis: port cache-key composition + write path (Wave-10 P1)'
labels: 'port, epic-07, wave-10'
---

## Source (TypeScript)

- File: `src/tools/lcm-synthesize-around-tool.ts` (relevant: cache-write path around lines 1330–1410, plus `leaf_fingerprint` helper and `session_key` fallback)
- File: `src/db/migration.ts` (UNIQUE index DDL for `lcm_synthesis_cache`)
- Lines: ~250 LOC of cache-write logic, plus the migration DDL (created in Epic 01 but the index discipline lives here)
- Function(s)/class(es): cache-key composition helper (`compute_cache_lookup(...) → dict`), `INSERT OR IGNORE` single-flight wrapper, `leaf_fingerprint(leaf_ids: Iterable[str]) → str` (SHA-256 first 24 hex chars)

## Target (Python)

- File: `src/lossless_hermes/synthesis/cache.py` (new file; not in the porting-guide's file-mapping table — created here to centralize the 7-field key composition)
- Estimated LOC: ~200

## What this issue covers

The 7-field cache key that backs `lcm_synthesis_cache`'s UNIQUE INDEX. Wave-10 P1 fix expanded the original 5-field index (`session_key, range_start, range_end, leaf_fingerprint, grep_filter`) to 7 fields by adding `tier_label` and `prompt_id`. Without those, `tier='custom'` and `tier='filtered'` collide for the same leaf set, and active-prompt updates silently serve stale text.

Key composition (all 7 fields required):

| Field | Type | Source | Notes |
|---|---|---|---|
| `session_key` | TEXT | 4-step fallback (per 07-05 parity item 11) | `targetSummary.session_key → input.session_key → resolved_conv[0].session_key → "agent:main:main"` |
| `range_start` | TEXT (ISO 8601 UTC) | window's start bound | `period` mode: computed; `time` mode: `target.created_at - window_hours`; `semantic` mode: earliest matched leaf's `created_at` |
| `range_end` | TEXT (ISO 8601 UTC) | window's end bound | same logic as `range_start` |
| `leaf_fingerprint` | TEXT (24 hex chars) | `hashlib.sha256("\0".join(leaf_ids).encode()).hexdigest()[:24]` | **Order-sensitive.** Different ordering = different fingerprint. |
| `grep_filter` | TEXT (nullable) | the grep pattern OR NULL | NULL and `""` unified via `COALESCE(grep_filter, '')` in the index |
| `tier_label` | TEXT | `req.tier` | one of `daily / weekly / monthly / yearly / custom / filtered` |
| `prompt_id` | TEXT | active-at-synthesis-time | FK → `lcm_prompt_registry.prompt_id`; when a prompt is updated (new version active), old cache rows become unreachable |

Single-flight pattern (cross-process coordination via the UNIQUE index):

```python
# LCM Wave-10 (2026-03-22): tier_label + prompt_id in cache UNIQUE index.
# Without these, tier='custom' then tier='filtered' for the same leaf set
# collided, and active-prompt updates silently served stale text.
# Original: lossless-claw/src/tools/lcm-synthesize-around-tool.ts:1340.
cache_id = secrets.token_hex(12)
cursor = conn.execute(
    """INSERT OR IGNORE INTO lcm_synthesis_cache
       (cache_id, session_key, range_start, range_end, leaf_fingerprint,
        grep_filter, tier_label, prompt_id, status, content, ...)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'building', NULL, ...)""",
    (cache_id, session_key, range_start, range_end, fingerprint,
     grep_filter, tier_label, prompt_id),
)
if cursor.rowcount == 0:
    # Lost the race; another caller is building. SELECT back to find it.
    existing = conn.execute(
        """SELECT cache_id, status, content FROM lcm_synthesis_cache
            WHERE session_key = ? AND range_start = ? AND range_end = ?
              AND leaf_fingerprint = ? AND COALESCE(grep_filter, '') = COALESCE(?, '')
              AND tier_label = ? AND prompt_id = ?""",
        (session_key, range_start, range_end, fingerprint,
         grep_filter, tier_label, prompt_id),
    ).fetchone()
    return _wait_for_or_join(existing)
```

After dispatch returns success: UPDATE `status='ready'`, `content=output`, write `lcm_cache_leaf_refs` rows (one per leaf in the source set; see 07-07).

## Dependencies

- Depends on: 07-05 (`SynthesisDispatcher` whose result this caches), 07-08 (prompt_id comes from `get_active_prompt`), Epic 01-06 (`lcm_synthesis_cache` table + 7-field UNIQUE index)
- Blocks: 07-07 (invalidation operates on the cache_id this writes), Epic 06 tools (`lcm_synthesize_around` is the caller)

## Acceptance criteria

- [ ] All 7 fields populated on every cache row write (Wave-10 P1)
- [ ] UNIQUE index in the migration matches the 7-field composition exactly (cross-check with Epic 01-06)
- [ ] `leaf_fingerprint(leaf_ids)` returns the first 24 hex chars of `SHA-256("\0".join(leaf_ids))` with order-sensitive joining (NOT sorted)
- [ ] Single-flight via `INSERT OR IGNORE` + on-zero-changes SELECT-back; both calls go through the same UNIQUE index
- [ ] `COALESCE(grep_filter, '')` parity between the WHERE clause and the UNIQUE index `COALESCE(grep_filter, '')` expression
- [ ] `cache_id` generated as `secrets.token_hex(12)` (24 hex chars; 96 bits — collision-free for the cache lifetime)
- [ ] `# LCM Wave-10 (2026-03-22): ...` inline comment on the index-bearing INSERT and on the `compute_cache_lookup` helper (per ADR-029)
- [ ] Order-sensitivity test: `leaf_fingerprint(["a", "b"]) != leaf_fingerprint(["b", "a"])`
- [ ] Collision test: `tier='custom'` and `tier='filtered'` for the same leaf set produce two distinct cache rows (Wave-10 P1 regression)
- [ ] `pytest tests/synthesis/test_cache.py` passes
- [ ] No new mypy errors with strict mode

## Tests to port

| Source | LOC | Cases |
|---|---:|---|
| `test/v41-wave10-reviewer-regressions.test.ts` (relevant subset) | — | (1) tier collision regression: `custom` + `filtered` → two rows; (2) active-prompt-update produces fresh cache row, old prompt_id's row stays for replay |
| `test/lcm-synthesize-around-tool.test.ts` (relevant subset) | — | (3) cache hit returns cached content without LLM call; (4) cache miss writes `status='building'` then `'ready'`; (5) single-flight: second caller during build returns the in-flight row |
| New tests this issue adds | — | (6) `leaf_fingerprint` order sensitivity; (7) `leaf_fingerprint` NUL-separator handling (a leaf ID with `\0` in it doesn't collide trivially — guard with a length check or reject); (8) session_key 4-step fallback unit test |

## Estimated effort

**3–4 hours.** Pure SQL + dataclass + a SHA-256 helper. Most cost is the parity regression test and the cross-check against Epic 01-06's migration DDL.

## Confidence

**92%.** Residual risk:

- **`leaf_fingerprint` NUL handling.** TS uses `\0` (null byte) as separator. If a leaf_id ever contains a literal `\0` (shouldn't happen — they're `s_<base36>` strings — but the schema doesn't enforce), the fingerprint becomes ambiguous. Mitigation: assert `"\0" not in leaf_id` at the helper boundary; raises early.
- **Cross-platform SHA-256 byte-identity.** Stdlib `hashlib.sha256` is identical across Python builds; no risk here unless a future MicroPython port needs to read the same DB.
