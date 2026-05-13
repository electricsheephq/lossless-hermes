---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-08] cli-ops: port lcm-doctor-shared.ts (contract surface)'
labels: 'port, epic-08-cli-ops'
---

## Source (TypeScript)

- File: `src/plugin/lcm-doctor-shared.ts`
- Lines: 270 LOC
- Function(s)/class(es): `detectDoctorMarker(content)`, `loadDoctorTargets(db, conversationId?)`, `getDoctorSummaryStats(db, conversationId?)`, exported constants (FALLBACK_SUMMARY_MARKER_*, TRUNCATED_SUMMARY_PREFIX, TRUNCATED_SUMMARY_WINDOW, FALLBACK_SUMMARY_WINDOW), exported types (DoctorMarkerKind, DoctorSummaryCandidate, DoctorConversationCounts, DoctorSummaryStats, DoctorTargetRecord)

## Target (Python)

- File: `src/lossless_hermes/doctor/shared.py` + `src/lossless_hermes/doctor/contract.py`
- Estimated LOC: ~320 (shared.py ~190 + contract.py ~130)

## What this issue covers

The shared doctor contract surface — three exported functions + several constants + four Pydantic models used by both `applyScopedDoctorRepair` (08-07) and `applyDoctorCleaners` (08-08). Per doctor-ops.md §"Doctor contract API (canonical)" line 31: "No file named `doctor-contract-api.d.ts` exists in the lossless-claw tree on `pr-613`. The 'formal contract' is the union of exported types and functions across the three plugin doctor modules."

The Python port consolidates the canonical types in `doctor/contract.py` (so 08-07 and 08-08 don't accidentally diverge on the contract); shared functions (marker detection, target loading, stats query) live in `doctor/shared.py`.

**`contract.py`** — Pydantic v2 models mirroring TS shapes:

```python
class DoctorMarkerKind(str, Enum):
    OLD = "old"; NEW = "new"; FALLBACK = "fallback"

class DoctorSummaryCandidate(BaseModel):
    conversation_id: int
    summary_id: str
    marker_kind: DoctorMarkerKind

class DoctorConversationCounts(BaseModel):
    total: int; old: int; truncated: int; fallback: int

class DoctorSummaryStats(BaseModel):
    candidates: list[DoctorSummaryCandidate]
    total: int; old: int; truncated: int; fallback: int
    by_conversation: dict[int, DoctorConversationCounts]

class DoctorTargetRecord(BaseModel):
    conversation_id: int
    summary_id: str
    kind: Literal["leaf", "condensed"]
    depth: int
    token_count: int
    content: str
    created_at: str
    child_count: int
    marker_kind: DoctorMarkerKind
```

Plus constants (verbatim string literals — CHANGING THESE IS A WIRE-PROTOCOL BREAK):

```python
FALLBACK_SUMMARY_MARKER = "[LCM fallback summary; truncated for context management]"
FALLBACK_SUMMARY_MARKER_V41_TRUNC = "[LCM fallback summary — model unavailable; raw source truncated for context management]"
FALLBACK_SUMMARY_MARKER_V41_FULL = "[LCM fallback summary — model unavailable; raw source preserved verbatim below]"
TRUNCATED_SUMMARY_PREFIX = "[Truncated from "
TRUNCATED_SUMMARY_WINDOW = 40
FALLBACK_SUMMARY_WINDOW = 80
```

**`shared.py`** — three functions per doctor-ops.md §"Doctor marker detection" lines 193–201:

1. **`detect_doctor_marker(content: str) -> DoctorMarkerKind | None`**
   - `FALLBACK` if content starts with `FALLBACK_SUMMARY_MARKER_V41_TRUNC` or `FALLBACK_SUMMARY_MARKER_V41_FULL` (v4.1 prefix form), OR if legacy `FALLBACK_SUMMARY_MARKER` appears as trailing suffix within last 80 chars.
   - `OLD` if content starts with `FALLBACK_SUMMARY_MARKER` (legacy prefix; defense-in-depth — unreachable on real data per the porting guide).
   - `NEW` if `TRUNCATED_SUMMARY_PREFIX` ("[Truncated from ") appears in the last 40 chars (trailing-suffix; "summary was emitted but content was truncated for size").
   - `None` otherwise.

2. **`load_doctor_targets(db, conversation_id: int | None = None) -> list[DoctorTargetRecord]`** — SELECT from `summaries` with a 4-marker INSTR pre-filter (matches the TS WHERE clause for performance), then re-runs `detect_doctor_marker` per row to classify. Ordering: `depth ASC, created_at ASC, summary_id ASC` (deterministic).

3. **`get_doctor_summary_stats(db, conversation_id: int | None = None) -> DoctorSummaryStats`** — returns aggregated counts grouped by conversation, plus the full candidate list.

## Dependencies

- Depends on: #08-01 (dispatcher), Epic 01-09 (`SummaryStore` schema).
- Blocks: #08-07 (doctor apply), #08-08 (doctor cleaners) — both consume these models + functions.

## Acceptance criteria

- [ ] All four pydantic models in `doctor/contract.py` match the TS shapes 1:1 (snake_case fields, same value semantics).
- [ ] All six constants (markers + windows) are verbatim string-equal to TS counterparts (test with `assert FALLBACK_SUMMARY_MARKER == "[LCM fallback summary; truncated for context management]"`).
- [ ] `detect_doctor_marker` returns the same value as TS for all four marker shapes (test fixture with ~20 cases covering each marker as prefix, suffix, within-window, beyond-window).
- [ ] `load_doctor_targets` returns rows in deterministic order (`depth ASC, created_at ASC, summary_id ASC`).
- [ ] `load_doctor_targets(db, conversation_id=42)` filters to that conversation; `load_doctor_targets(db)` (no filter) returns DB-wide.
- [ ] Pre-filter INSTR clause in SQL matches the TS query (validated by `EXPLAIN QUERY PLAN` snapshot — uses partial index where applicable).
- [ ] `get_doctor_summary_stats` returns `total`, `old`, `truncated`, `fallback` per conversation in `by_conversation` map; per-summary candidates in `candidates`.
- [ ] No dedicated TS tests exist for these functions (doctor-ops.md §"Test inventory" line 430: "Doctor cleaners and `applyScopedDoctorRepair` have no dedicated test file on this branch... this is a coverage gap worth filling in the Python port.").
- [ ] **New test:** `tests/doctor/test_shared.py::test_detect_doctor_marker` — 20-case fixture for marker detection.
- [ ] **New test:** `tests/doctor/test_shared.py::test_load_doctor_targets_ordering` — seeded DB with mixed depth/created_at, asserts ordering.
- [ ] **New test:** `tests/doctor/test_shared.py::test_get_doctor_summary_stats_per_conversation` — 3-conversation fixture, asserts per-conv aggregation.
- [ ] Function signatures match the spec in [docs/porting-guides/doctor-ops.md](../../docs/porting-guides/doctor-ops.md) §"Doctor contract API (canonical)" lines 86–88.
- [ ] `pytest tests/doctor/test_shared.py` passes.
- [ ] No new mypy errors (`mypy --strict src/lossless_hermes/doctor/shared.py src/lossless_hermes/doctor/contract.py`).
- [ ] PR description cites LCM commit `1f07fbd` (pr-613 head).

## Estimated effort

**4 hours.**

## Confidence

**95%** — pure read-only translation; marker constants are verbatim string literals; detection logic is well-specified.
