# ADR-037: Defer `lcm_expand` to post-v0.2.0 — remove its schema from the registry

**Status:** Accepted
**Date:** 2026-05-19
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —
**Implementation:** v0.2.0-tracked (PR-0 of the issue #156 dispatch-adapter sequence)
**Issue:** [electricsheephq/lossless-hermes#156](https://github.com/electricsheephq/lossless-hermes/issues/156)

## Context

`lossless-hermes` ships nine `lcm_*` agent-tool schemas to the model: seven
ports of the LCM TypeScript tool factories plus the two ADR-035 diagnostic
tools (`lcm_status`, `lcm_doctor`). A P0 found during the #155 review (issue
#156) established that the seven ported tools' schemas were registered into
`TOOL_SCHEMAS` but never wired into `TOOL_DISPATCH` — the core agent-tool
feature is non-functional. The scoping investigation for #156 produced a
per-tool build plan and a four-PR sequence (PR-0 → PR-3) to wire a
dispatch-adapter layer.

That investigation reached a definitive verdict on one tool: **`lcm_expand`
cannot be wired in the v0.2.0 dispatch-adapter window.** Its handler
(`tools/expand.py::handle_lcm_expand`) takes a strict, keyword-only typed
`ctx: ExpandContext` Protocol. `ExpandContext` (`expand.py:426-449`) requires
two collaborator members the engine cannot supply:

- `orchestrator: ExpansionOrchestrator` — Protocol-only (`expand.py:372`); no
  concrete production implementation exists anywhere in `src/`.
- `retrieval: Retrieval` — Protocol-only (`expand.py:399`); likewise no
  concrete implementation in `src/`.

Building these collaborators is not a dispatch-adapter task. It requires
porting roughly **1,175 LOC of unported TypeScript** from `lossless-claw`
(`pr-613` @ `1f07fbd`):

- `retrieval.ts` — `class RetrievalEngine`, ~424 LOC.
- `expansion.ts` — `class ExpansionOrchestrator`, ~386 LOC.
- `expansion-auth.ts` — the sub-agent grant ledger (`expansion_auth`), ~365 LOC.

That is a multi-issue epic in its own right, not a PR in the #156
dispatch-adapter sequence.

There is a second, independent reason `lcm_expand` cannot ship usefully now.
`lcm_expand` is **sub-agent-only**: its own model-facing description begins
"SUB-AGENT ONLY. Main-agent sessions get a runtime error if they invoke this
tool." The handler refuses every main-agent call. The only path by which a
*granted* sub-agent can exist is the delegated-expansion grant established by
the sub-agent spawn API — and that path is itself **deferred per ADR-012**
(`prepareSubagentSpawn` / `onSubagentEnded` have no Hermes equivalent;
`lcm_expand_query`, the delegation entry point, is not registered). With
delegation deferred, no granted sub-agent can ever exist, so even a fully
wired `lcm_expand` would refuse 100% of calls it could receive.

The constraint forcing a decision: a tool whose schema is advertised to the
model but which cannot function is exactly the #156 bug. We must either ship
`lcm_expand` working, or stop advertising it. Shipping it working is blocked
on ~1,175 LOC of unported code and on the ADR-012-deferred delegation path.

> **Note — this ADR is a decision record, not the decision.** The project
> lead has already decided to defer `lcm_expand`; the #156 scoping plan
> records the verdict ("`lcm_expand` must be deferred behind an ADR"). This
> ADR documents that decision and its rationale so the deferral has the same
> auditable provenance as ADR-012 (`lcm_expand_query`) and ADR-030
> (stub-tier).

## Options considered

### Option A: Defer `lcm_expand`; remove `LCM_EXPAND_SCHEMA` from the registry

- **Description:** Stop appending `LCM_EXPAND_SCHEMA` to `TOOL_SCHEMAS` so
  `get_tool_schemas()` returns eight tools (six ported + `lcm_status` +
  `lcm_doctor`), not nine. The schema constant `LCM_EXPAND_SCHEMA`, the
  handler `handle_lcm_expand`, and the whole `tools/expand.py` module are
  **retained** for the future port — only the registry advertisement is
  removed. The `expand` module is still imported by `tools/__init__.py` (so
  the handler and schema stay reachable) but its module-level
  `TOOL_SCHEMAS.append(...)` side-effect is dropped. Re-wiring `lcm_expand` is
  tracked as a separate post-v0.2.0 epic that ports `retrieval.ts`,
  `expansion.ts`, and `expansion-auth.ts`. The #156 dispatch-adapter sequence
  proceeds with an 8/8 coverage target.
- **Pros:**
  - **Removes the #156 bug for `lcm_expand`.** An unusable tool is no longer
    advertised to the model. The model cannot select a tool that does not
    exist in the schema list, so the failure mode (model calls `lcm_expand`,
    gets a crash or a structured error) is eliminated at the source.
  - **Honest tool surface.** The eight advertised tools are exactly the eight
    that the #156 sequence can make functional. No advertised tool is a dead
    end.
  - **No code thrown away.** `handle_lcm_expand` + `tools/expand.py` +
    `LCM_EXPAND_SCHEMA` survive verbatim. The future epic re-adds one
    `TOOL_SCHEMAS.append(...)` line and the dispatch-adapter wiring; nothing
    has to be re-ported.
  - **Follows established precedent.** ADR-012 deferred `lcm_expand_query`
    behind an ADR and left its description in the verbatim fixture; ADR-030
    deferred the stub-tier feature behind an ADR. Deferring `lcm_expand` the
    same way keeps the project's defer-with-ADR discipline intact.
  - **Unblocks the #156 sequence.** PR-1..PR-3 wire the six wireable ported
    tools against a fixed, achievable 8/8 coverage target instead of a 9/9
    target one tool can never reach.
- **Cons:**
  - **The model loses `lcm_expand` until the epic lands.** This is a real
    capability gap — but the capability is *already* unavailable today
    (the tool never dispatched) and is *additionally* dead without ADR-012
    delegation. The deferral makes an existing gap explicit rather than
    creating a new one.
  - **A follow-up epic must be tracked** so the deferral does not become a
    permanent silent omission.
- **Evidence cited:**
  - Issue #156 scoping plan §1 — `ExpansionOrchestrator` / `Retrieval` /
    grant-ledger are Protocol-only; ~1,175 LOC of unported TS; verdict "defer
    `lcm_expand` behind a new ADR".
  - `tools/expand.py:372,399` — `ExpansionOrchestrator` and `Retrieval` are
    `Protocol` declarations with no concrete `src/` implementation.
  - `tools/expand.py` `LCM_EXPAND_DESCRIPTION` — "SUB-AGENT ONLY. Main-agent
    sessions get a runtime error if they invoke this tool."
  - ADR-012 — `lcm_expand_query` / sub-agent delegation deferred; the grant
    path `lcm_expand` depends on does not exist.
  - ADR-012, ADR-030 — the established defer-with-ADR precedent.

### Option B: Wire `lcm_expand` with stub `ExpansionOrchestrator` / `Retrieval`

- **Description:** Build no-op or minimal stub implementations of the two
  Protocol collaborators so `lcm_expand` can be registered in `TOOL_DISPATCH`
  within the v0.2.0 window, deferring the real port.
- **Pros:**
  - The 9/9 coverage target is nominally met inside v0.2.0.
- **Cons:**
  - **Advertises a permanently-failing tool — the #156 bug, unfixed.** A tool
    backed by stubs returns errors or empty results for every call. The model
    pays selection cost and a round-trip for a tool that cannot do its job.
    Removing the advertisement (Option A) is the correct fix; a stub is the
    bug wearing a coverage checkmark.
  - **Sub-agent-only refusal still applies.** Even a fully stubbed
    `lcm_expand` refuses every main-agent call, and no granted sub-agent can
    exist without ADR-012 delegation. The stub does not make the tool
    callable.
  - **Stub code is throwaway and a future trap.** A stub `ExpansionOrchestrator`
    that silently returns empty is worse than no orchestrator — it invites a
    future contributor to "just use the existing one" without porting the
    real `expansion.ts`.
  - **Diverges from ADR-030.** ADR-030 explicitly chose *not* to ship the
    stub-tier on a partial basis; shipping `lcm_expand` on stubs would
    contradict that posture.

### Option C: Keep `lcm_expand` registered, unwired, and crash-hardened

- **Description:** Leave `LCM_EXPAND_SCHEMA` in `TOOL_SCHEMAS`, never register
  a `TOOL_DISPATCH` handler, and rely on PR-0's crash-hardening wrapper to
  convert the resulting failure into a structured tool-error.
- **Pros:**
  - No registry change; the schema fixture surface is unchanged.
- **Cons:**
  - **Still the #156 bug.** The model still sees `lcm_expand` in its tool list
    and will still select it; the crash-hardening wrapper degrades the failure
    from a crash to a structured error, but the tool is still advertised and
    still non-functional. PR-0's hardening is a safety net for the *rollout*,
    not a license to advertise dead tools.
  - **Wastes model context and turns.** Every `lcm_expand` selection is a
    wasted round-trip ending in an error the model must then reason around.
  - **No honest signal.** A tool list should describe what the agent can do.
    Listing a tool that always fails is misinformation.

## Decision

Chosen: **Option A — defer `lcm_expand` to post-v0.2.0 and remove
`LCM_EXPAND_SCHEMA` from the `TOOL_SCHEMAS` registry.** `get_tool_schemas()`
returns the eight-tool surface (`lcm_grep`, `lcm_describe`, `lcm_get_entity`,
`lcm_search_entities`, `lcm_compact`, `lcm_synthesize_around`, `lcm_status`,
`lcm_doctor`). `handle_lcm_expand`, `LCM_EXPAND_SCHEMA`, and the entire
`tools/expand.py` module are **retained** for the future port; only the
import-time `TOOL_SCHEMAS.append(LCM_EXPAND_SCHEMA)` advertisement is removed.
The `lcm_expand` re-wiring — porting `retrieval.ts`, `expansion.ts`, and
`expansion-auth.ts` (~1,175 LOC) — is tracked as a separate post-v0.2.0 epic
and is gated on the ADR-012-deferred sub-agent delegation path.

This is implemented in **PR-0 of the issue #156 dispatch-adapter sequence**,
which must merge before any adapter PR (PR-1..PR-3). The #156 coverage target
is **8/8**, not 9/9.

## Rationale

1. **An unusable tool must not be advertised — that is the #156 bug itself.**
   The model selects tools from the schema list. A schema for a tool that
   cannot dispatch (Option C) or that always fails on stubs (Option B) is
   misinformation that costs the model context and round-trips. Removing the
   advertisement (Option A) is the only option that actually fixes the bug
   for `lcm_expand`.

2. **Wiring `lcm_expand` properly is a multi-issue epic, not a PR.** The two
   collaborators `ExpandContext` requires — `ExpansionOrchestrator` and
   `Retrieval` — are Protocol-only with no concrete `src/` implementation.
   Building them means porting ~1,175 LOC of unported TypeScript
   (`retrieval.ts` ~424, `expansion.ts` ~386, `expansion-auth.ts` ~365). That
   work does not belong in the #156 dispatch-adapter sequence, whose other
   PRs are name/shape translation against collaborators that already exist.

3. **`lcm_expand` is operationally dead without ADR-012 delegation anyway.**
   The tool is sub-agent-only and refuses every main-agent call. The only way
   a granted sub-agent can exist is the delegated-expansion grant — and that
   path is deferred per ADR-012. Even a fully wired `lcm_expand` would refuse
   100% of the calls it could receive. Deferring it now makes an already-dead
   tool explicitly deferred rather than silently broken.

4. **No code is lost.** `handle_lcm_expand` and `tools/expand.py` are kept
   verbatim. The future epic re-adds exactly one `TOOL_SCHEMAS.append(...)`
   line plus the dispatch-adapter wiring. The deferral costs the project
   nothing it will have to re-do.

5. **It follows the project's established defer-with-ADR discipline.**
   ADR-012 deferred `lcm_expand_query` (the sub-agent convenience tool) behind
   an ADR; ADR-030 deferred the PR-628 stub-tier behind an ADR. Both left the
   deferred surface documented and recoverable. Deferring `lcm_expand` the
   same way — ADR + retained code + tracked follow-up — keeps that discipline
   intact and gives the deferral auditable provenance.

Option B (stub collaborators) was rejected because it ships the #156 bug
wearing a coverage checkmark — a permanently-failing advertised tool — and
contradicts ADR-030's no-partial-ship posture. Option C (registered but
unwired) was rejected because it leaves the tool advertised and
non-functional; PR-0's crash-hardening is a rollout safety net, not a license
to advertise dead tools.

## Consequences

- **`LCM_EXPAND_SCHEMA` is removed from `TOOL_SCHEMAS`.** `tools/expand.py` no
  longer runs `TOOL_SCHEMAS.append(LCM_EXPAND_SCHEMA)` at import time, and no
  longer imports `TOOL_SCHEMAS`. `get_tool_schemas()` returns **8** tools, not
  9. The model-facing tool surface is `lcm_grep`, `lcm_describe`,
  `lcm_get_entity`, `lcm_search_entities`, `lcm_compact`,
  `lcm_synthesize_around`, `lcm_status`, `lcm_doctor`.
- **`handle_lcm_expand` and `tools/expand.py` are retained.** The handler, the
  `LCM_EXPAND_SCHEMA` constant, the `ExpandContext` Protocol, and the whole
  module survive for the future port. `tools/__init__.py` still imports the
  `expand` module (so the handler/schema stay reachable) — the import simply
  no longer has a registration side-effect.
- **The #156 dispatch-adapter sequence targets 8/8 coverage.** PR-1..PR-3 wire
  the six wireable ported tools; the registry↔dispatch regression test
  (`tests/test_dispatch_registry_coverage.py`) is parametrized over the
  eight-tool surface, and a `test_lcm_expand_deferred` case asserts
  `lcm_expand` is absent from both `TOOL_DISPATCH` and `get_tool_schemas()`.
- **`lcm_expand` re-wiring is a tracked post-v0.2.0 epic.** Re-instating the
  tool requires porting `retrieval.ts`, `expansion.ts`, and `expansion-auth.ts`
  (~1,175 LOC) and is additionally gated on the ADR-012-deferred sub-agent
  delegation path. The deferral must be tracked as a follow-up epic so it does
  not become a permanent silent omission.
- **The verbatim-description fixture is unchanged.** `lcm_expand` stays in
  `tests/fixtures/lcm_v4.1_tool_descriptions.json` and keeps its SHA-256 lock
  in `tests/tools/test_descriptions_verbatim.py::_DESCRIPTION_SHA256` — the
  fixture is the TS-source snapshot and the deferral is a registration policy,
  not a source change. The verbatim lint
  (`test_every_registered_tool_description_matches_fixture`) parametrizes over
  the *registered* schemas, so dropping `lcm_expand` from the registry simply
  drops one parametrized case; `test_no_missing_tools_registered`'s
  `_EXPECTED_TOOL_NAMES` constant is updated to the eight-tool set with this
  ADR cited.
- **Invariant:** no tool is advertised in `TOOL_SCHEMAS` unless it has (or, in
  the #156 sequence, is about to have) a working `TOOL_DISPATCH` entry. A
  schema in the registry is a promise to the model that the tool works.
- **Invariant:** `tools/expand.py` and `handle_lcm_expand` are not deleted
  while the deferral stands. Deleting them would force a re-port; the deferral
  is a registration change, not a code removal.

## Open questions / 5% uncertainty

1. **Scope of the `lcm_expand` re-wiring epic.** The ~1,175 LOC estimate
   (`retrieval.ts` + `expansion.ts` + `expansion-auth.ts`) is from the #156
   scoping plan and is a planning figure, not a committed issue breakdown. The
   epic that re-instates `lcm_expand` owns the precise issue split; it must
   also resolve the ADR-012 delegation dependency first (a granted sub-agent
   cannot exist until delegation ships).
2. **Whether `lcm_expand` and `lcm_expand_query` un-defer together.** Both are
   sub-agent-only and both depend on the ADR-012 delegation path. It is
   plausible the future delegation epic un-defers both at once; this ADR does
   not bind that sequencing — it defers `lcm_expand` only, and the delegation
   epic decides whether to bundle them.
