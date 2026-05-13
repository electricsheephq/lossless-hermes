---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-N] <subsystem>: <verb> <object>'
labels: 'port'
---

## Source (TypeScript)
- File: `src/...`
- Lines: ~LOC
- Function(s)/class(es): ...

## Target (Python)
- File: `src/lossless_hermes/...`
- Estimated LOC: ...

## Dependencies
- Depends on: #X (must be merged first)
- Blocks: #Y, #Z

## Acceptance criteria
- [ ] All TS unit tests in `test/<file>.test.ts` have ported pytest equivalents
- [ ] Function signatures match the spec in [docs/porting-guides/<subsystem>.md](../../docs/porting-guides/<subsystem>.md)
- [ ] `pytest tests/<file>` passes
- [ ] No new mypy errors
- [ ] PR description cites the LCM commit SHA being ported

## Estimated effort
N hours

## Confidence
% — note any remaining uncertainty
