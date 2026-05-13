---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-01] storage: port large-files.ts → large_files.py'
labels: 'port, epic-01-storage'
---

## Source (TypeScript)

- File: `src/large-files.ts`
- Lines: **567 LOC**
- Function(s)/class(es): `<file>` block parser, MIME → extension mapping, deterministic exploration summaries (JSON/CSV/code), `file_<sha>` ID extraction.

## Target (Python)

- File: `src/lossless_hermes/large_files.py`
- Estimated LOC: ~650

## What this issue covers

The non-DB on-disk file storage layer + the deterministic-summary path used by the assembler when a tool result contains a `<file>` block (or when an embedded file in a user message would exceed `largeFileTokenThreshold`).

### Behavioral spec

Per storage.md §1 row 16 and `docs/porting-guides/storage.md` §"Path mapping" → Python target = `src/lossless_hermes/large_files.py`.

This module is **pure logic — no direct DB access** (storage.md Appendix A row last). It produces `LargeFileRecord` shapes that callers (SummaryStore #01-09 via `insert_large_file`) persist.

### Pieces to port

1. **`<file>` block extraction** — given a message body, find `<file path="..." mime="...">...</file>` blocks (or the LCM canonical form — verify exact regex in source). Return parsed metadata + content.

2. **MIME → extension mapping** — table-driven; pulls from a constant dictionary. Used to choose the on-disk filename suffix when writing the file out to `$largeFilesDir`.

3. **File ID extraction** — `file_<sha>` ID pattern. Parsed from existing references in conversation history so re-uploads of the same file are detected.

4. **SHA-256 content hashing** — for ID generation. Use `hashlib.sha256`.

5. **Deterministic exploration summaries** — given the file content, produce a human-readable summary:
   - **JSON** — key path enumeration + sample values (per the LCM heuristic; verify in source).
   - **CSV** — column headers + row count + first-3-row sample.
   - **Code** — language-aware sketch (per file extension): imports + function/class names + LOC counts.
   - **Plain text** — first N tokens, last N tokens, total token count.

6. **Write-to-disk path** — write content to `<largeFilesDir>/<file_id>.<ext>` atomically (write to tmp + rename). Set restrictive `chmod 0o600` (per the OpenClaw security practice; verify in source).

7. **Read-from-disk path** — read by file_id, validate the SHA matches, return content.

### Out of scope

- DB writes — `summary.py.insert_large_file` (#01-09) consumes the records this module produces.
- The PR #628 `lcm-blob-migrate.mjs` externalization migration — out of scope per storage.md §5 + ADR-030.
- The `<file>` block detection inside the engine (`extractMessageContent` at `src/engine.ts:765-788`) — that's Epic 02.

## Dependencies

- Depends on: #01-02 (config — reads `large_files_dir` and `large_file_token_threshold`), #00-01 (scaffolding).
- Blocks: #01-09 (SummaryStore.`insert_large_file` consumes these records).
- **Parallel-portable** with #01-08, #01-09, #01-14 (storage.md §9 last paragraph) — different engineer can take this in parallel after Phase 3 lands.

## Acceptance criteria

- [ ] `<file>` block parser handles the canonical LCM block format (verify exact format in source).
- [ ] MIME → extension lookup covers at least: `text/plain → txt`, `application/json → json`, `text/csv → csv`, `text/markdown → md`, `text/x-python → py`, `application/javascript → js`, `text/x-typescript → ts`, `application/octet-stream → bin`. Include the full table from the TS source.
- [ ] `file_<sha>` ID extraction returns all existing file references in a message body (test with 0, 1, and 3 inline references).
- [ ] Deterministic exploration summaries are byte-identical to the TS output on a parity fixture (port the TS test inputs verbatim and assert equality).
- [ ] Write-to-disk is atomic (verified via interruption test: kill mid-write, no partial file in `largeFilesDir`).
- [ ] Read-from-disk validates SHA; mismatch raises `LargeFileIntegrityError`.
- [ ] File permissions are `0o600` after write (verified via `os.stat`).
- [ ] All **8 TS test cases** in `test/large-files.test.ts` (storage.md §8 row 18 — block parsing, MIME→ext, file-id extraction, exploration summaries) port to `tests/test_large_files.py`.
- [ ] `pytest tests/test_large_files.py` passes.
- [ ] `mypy --strict` passes.
- [ ] PR description cites LCM commit `1f07fbd` and `src/large-files.ts` (567 LOC).

## Estimated effort

**8 hours.**

## Confidence

**92%** — pure logic, well-tested. Residual risk: the deterministic-summary heuristics for code files (language detection + import enumeration) have many edge cases; the parity-fixture-vs-TS-output assertion handles regression detection.
