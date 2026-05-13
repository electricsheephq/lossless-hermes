# lossless-hermes

Lossless Context Management for **Hermes-agent**, ported from [Martian-Engineering/lossless-claw](https://github.com/Martian-Engineering/lossless-claw) (TypeScript/OpenClaw) to Python/Hermes.

## Status: 🟡 Phase 1 — Architecture & Planning

The port is feasible. Architecture, decisions, risks, and full epic/issue breakdown live under [`docs/`](./docs/) and [`epics/`](./epics/). Phase 2 (execution) starts when every architecture decision in [`docs/adr/`](./docs/adr/) is at 95%+ confidence.

## Source of truth

| Document | Purpose |
|---|---|
| [`ROADMAP.md`](./ROADMAP.md) | 10-epic roadmap, milestones, critical path |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | System architecture, target structure, data flow |
| [`docs/risks.md`](./docs/risks.md) | Identified risks + mitigation status |
| [`docs/adr/`](./docs/adr/) | Architecture Decision Records (numbered, dated, status-tagged) |
| [`docs/porting-guides/`](./docs/porting-guides/) | Per-subsystem TS → Python porting guides |
| [`docs/reference/`](./docs/reference/) | Cross-reference docs (source map, hook reference) |
| [`docs/spike-results/`](./docs/spike-results/) | De-risking spike findings |
| [`epics/`](./epics/) | 10 epics, each with issue specifications |

## Project context

- **Source**: `Martian-Engineering/lossless-claw` main + [PR #613](https://github.com/Martian-Engineering/lossless-claw/pull/613) (v4.1 omnibus, 52k LOC) + [PR #628](https://github.com/Martian-Engineering/lossless-claw/pull/628) (stub-tier, merged)
- **Target**: `NousResearch/hermes-agent` Python plugin via `ContextEngine` ABC
- **OpenClaw coupling surface**: 26 LOC in `src/openclaw-bridge.ts` (single import seam)
- **Hermes anticipation**: `agent/context_engine.py` docstring at line 5 explicitly names LCM as a planned tenant

## Quick links

- Hermes-agent repo: https://github.com/NousResearch/hermes-agent
- Source repo: https://github.com/Martian-Engineering/lossless-claw
- PR #613 (omnibus): https://github.com/Martian-Engineering/lossless-claw/pull/613
- PR #628 (stub-tier): https://github.com/Martian-Engineering/lossless-claw/pull/628
- Hermes ContextEngine ABC: https://github.com/NousResearch/hermes-agent/blob/main/agent/context_engine.py
