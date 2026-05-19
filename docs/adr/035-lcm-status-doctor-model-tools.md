# ADR-035: Expose `lcm_status` / `lcm_doctor` as model-callable diagnostic tools

**Status:** Accepted
**Date:** 2026-05-19
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —
**Implementation:** v0.2.0-tracked
**Issue:** [electricsheephq/lossless-hermes#135](https://github.com/electricsheephq/lossless-hermes/issues/135)

## Context

`lossless-hermes` exposes its health surface — `/lcm status` (full LCM health
snapshot; `commands/status.py`) and `/lcm doctor` (read-only integrity scan;
`commands/doctor.py::run_scan`) — **only as `/lcm` slash subcommands**. Slash
commands are operator-facing: the dispatcher is reached through Hermes's
`register_command` hook, and the output lands in the human operator's terminal.
The **model** running inside a turn has no way to observe LCM's own state.

That matters because LCM degrades in ways the model can otherwise only infer
indirectly:

- A synthesis pass falls back to a degraded summary (circuit breaker open).
- Context pressure pushes the assembler into deeper eviction than usual.
- An integrity check fails (orphaned summaries, a NULL `identity_hash` row, a
  broken `WITH RECURSIVE` subtree).

When any of these happens, the model sees a *symptom* — a recall miss, a
summary that reads thin — but cannot tell whether the cause is its own prompt
or a degraded LCM substrate. It has no self-diagnosis path mid-task.

The architecture review against the sibling project (`hermes-lcm`, a 540★
independent Python LCM plugin) flagged this in slice **S12** (95% confidence):
`hermes-lcm` ships `lcm_status` + `lcm_doctor` as **model-callable agent
tools**, not just slash commands, so the agent can detect its own degradation
and self-diagnose after a weird recall miss. `lossless-hermes` ships eight
`lcm_*` tools today (`tools/__init__.py` registry) — none of them is a
diagnostic.

The constraint forcing a choice: v0.1.0 ships health as operator-only slash
commands. Do we leave it there, or add read-only model-callable diagnostic
tools so the agent can self-diagnose mid-turn?

## Options considered

### Option A: Add `lcm_status` + `lcm_doctor` as read-only model-callable tools

- **Description:** Add two new entries to the `lcm_*` tool registry. Each wraps
  the **existing** command body — `lcm_status` wraps `commands/status.py`'s
  status-text builder; `lcm_doctor` wraps `commands/doctor.py::run_scan` (the
  read-only scan arm). No new diagnostic logic: the tool handler calls the same
  function the slash handler already calls and returns its rendered text.
  `LCM_STATUS_SCHEMA` / `LCM_DOCTOR_SCHEMA` are registered in
  `tools/__init__.py` alongside the existing eight schemas. Both schemas take
  **no parameters** (empty `parameters` object). Neither is owner-gated —
  they are strictly read-only (a status snapshot and an integrity *scan*; no
  mutation). The `/lcm` slash commands stay exactly as they are, and they
  remain the **only** surface for the write paths (`/lcm doctor apply`,
  `/lcm purge`, `/lcm reconcile`).
- **Pros:**
  - **Closes the self-diagnosis gap.** The model can call `lcm_status` after a
    recall miss and see context pressure / fallback-summary state directly,
    rather than guessing. This is exactly the `hermes-lcm` behaviour S12 cites.
  - **Zero new logic, minimal blast radius.** The handlers are thin wrappers
    over already-tested code (`commands/status.py`, `commands/doctor.py`). The
    risk surface is the schema wiring and output capping, not the diagnostics.
  - **Read-only ⇒ no owner gate ⇒ no new auth surface.** Per ADR-013, owner
    gating exists for *destructive* `/lcm` subcommands. A status snapshot and a
    read-only scan mutate nothing, so the gate that protects `doctor apply` /
    `purge` / `reconcile` does not apply. No new policy code.
  - **Symmetry with the existing tool registry.** The `lcm_*` tool surface is
    already the model's window into LCM; adding two diagnostics there is the
    consistent place for them. The well-formedness test in
    `tests/tools/test_schemas_wellformed.py` is parametrized over the registry,
    so it auto-extends to the two new schemas.
  - **Slash commands keep the write paths.** Operators retain the full
    surface; nothing is removed. The model gains read-only visibility only.
- **Cons:**
  - **Tool-result budget risk.** `/lcm status` renders a multi-section markdown
    report; `/lcm doctor`'s scan can enumerate many findings. Dumped verbatim
    into a tool result, this can consume a large slice of the turn's
    tool-result budget — the model pays context for the diagnostic. Mitigation
    is mandatory (see Consequences): the tool-variant output is **capped and/or
    summarized** to a bounded size, distinct from the unbounded operator-facing
    slash output.
  - **Two render paths to keep coherent.** The slash variant renders the full
    report; the tool variant renders a capped digest. They share the same
    underlying data-collection function but diverge at the formatting layer —
    a small, contained amount of duplication.
- **Evidence cited:**
  - Architecture review slice S12 (95%): `hermes-lcm` ships `lcm_status` +
    `lcm_doctor` as model tools so the agent self-diagnoses mid-task.
  - `tools/__init__.py` — the existing `LCM_<TOOL>_SCHEMA` registry pattern
    (`TOOL_SCHEMAS`, `get_tool_schemas`); per-tool modules append at import.
  - `commands/status.py`, `commands/doctor.py` — existing command bodies the
    tool handlers wrap; `run_scan` is the read-only doctor arm.
  - ADR-013 (owner gating) — gate scope is *destructive* subcommands only.

### Option B: Keep status/doctor operator-only (status quo)

- **Description:** Leave health as `/lcm` slash subcommands. The model never
  sees LCM's own state; only a human operator does.
- **Pros:**
  - No new code. No tool-result budget concern. The v0.1.0 surface is
    unchanged.
  - One render path per command.
- **Cons:**
  - **The self-diagnosis gap stays open.** The model cannot distinguish "my
    prompt is wrong" from "LCM handed me a degraded substrate." Every weird
    recall miss is unattributable from inside the turn.
  - **Diverges from the production sibling.** `hermes-lcm` ships these as
    tools; an operator comparing the two sees `lossless-hermes` as the one
    where the agent is blind to its own context engine.
  - **Pushes diagnosis to the human.** Self-healing / self-explaining agent
    behaviour is not possible — a recall miss can only be debugged after the
    fact by an operator running `/lcm status` themselves.

### Option C: One combined `lcm_diagnostics` tool

- **Description:** A single model tool that returns status + doctor-scan output
  together in one call.
- **Pros:**
  - One schema, one registry entry, one round-trip for the model.
- **Cons:**
  - **Worst tool-result budget profile.** Status *and* a full integrity scan
    in one result is the largest possible diagnostic payload — the exact thing
    the budget caveat warns against.
  - **No parity with `hermes-lcm`'s two named tools** — slice S12 cites
    `lcm_status` and `lcm_doctor` as distinct tools; a merged tool diverges
    from the surface the review measured.
  - **Forces a scan on every status check.** The model often only wants the
    cheap status snapshot; bundling the scan makes the common case pay for the
    expensive case. Two tools let the model pick the cheap one.

## Decision

Chosen: **Option A — add `lcm_status` + `lcm_doctor` as two read-only
model-callable tools, wrapping the existing `commands/status.py` +
`commands/doctor.py` (read-only `run_scan`) bodies. `LCM_STATUS_SCHEMA` /
`LCM_DOCTOR_SCHEMA` registered in `tools/__init__.py` with empty-parameter
schemas and no owner gate. `/lcm` slash commands stay, and remain the only
surface for the write paths.**

Implementation is **v0.2.0-tracked** (issue #135). This ADR is the decision
record; the schema + handler wiring lands as a v0.2.0 issue.

## Rationale

1. **The self-diagnosis gap is the whole point.** LCM degrades in ways the
   model otherwise can only infer from symptoms. A read-only `lcm_status` call
   lets the model see fallback-summary state and context pressure directly,
   and a read-only `lcm_doctor` scan lets it see integrity failures — turning
   an unattributable recall miss into a diagnosable one *inside the turn*.
   This is exactly the behaviour the review's slice S12 (95%) cites
   `hermes-lcm` for.

2. **It is a near-zero-logic change.** The two handlers wrap command bodies
   that already exist and are already tested (`commands/status.py`,
   `commands/doctor.py::run_scan`). No diagnostic algorithm is written; only
   schema wiring and output capping. The risk surface is small and contained.

3. **Read-only means no new auth surface.** Per ADR-013, owner gating protects
   *destructive* `/lcm` subcommands (`doctor apply`, `purge`, `reconcile`).
   `lcm_status` is a snapshot and `lcm_doctor` is a read-only *scan* — neither
   mutates state, so the gate does not apply and no new policy code is needed.
   The destructive arms stay slash-only and stay gated.

4. **Two tools, not one, is the right granularity.** Status is cheap; a full
   integrity scan is not. Separate tools let the model call the cheap snapshot
   for the common case and reach for the scan only when it needs it — and it
   matches the two named tools (`lcm_status`, `lcm_doctor`) the review
   measured on `hermes-lcm`. Option C's merged tool forces every status check
   to pay for a scan and diverges from that surface.

5. **The registry pattern already supports this cleanly.** `tools/__init__.py`
   exposes `TOOL_SCHEMAS` + `get_tool_schemas`; per-tool modules append their
   `LCM_<TOOL>_SCHEMA` at import time, and the well-formedness test is
   parametrized over the registry. Two new diagnostic schemas extend that
   surface without a structural change.

Option B (status quo) was rejected because it leaves the model blind to its
own context engine — the exact gap the review found. Option C (one combined
tool) was rejected for the worst tool-result budget profile and for diverging
from `hermes-lcm`'s two-tool surface.

## Consequences

- **`tools/__init__.py` gains two schemas.** `LCM_STATUS_SCHEMA` and
  `LCM_DOCTOR_SCHEMA` are registered alongside the existing eight via the same
  import-time `TOOL_SCHEMAS.append(...)` pattern. New per-tool modules
  (`tools/status.py`, `tools/doctor.py`, or equivalent) host the schema +
  handler; the import order in `tools/__init__.py` determines registration
  order, consistent with the existing modules.
- **Both schemas take no parameters.** The `parameters` object is empty (an
  `object_schema` with no fields). A status snapshot and a whole-DB integrity
  scan need no model-supplied input — the tool operates on the engine's
  current conversation and DB. Empty-param schemas keep the model's call site
  trivial and unambiguous.
- **Neither tool is owner-gated.** They are read-only. ADR-013's gate, and the
  upstream `SlashAccessPolicy` check that fronts the destructive `/lcm`
  subcommands, do not participate. No new authorization code.
- **Tool-variant output is capped/summarized — mandatory.** This is the
  load-bearing caveat. The slash-command variant may render the full,
  unbounded report (an operator's terminal can take it). The **tool** variant
  MUST cap or summarize its output to a bounded size so a diagnostic call does
  not blow the turn's tool-result budget. Concretely: cap `lcm_status` to its
  highest-signal sections (config, global counts, current-conversation
  pressure, doctor-summary line) and cap `lcm_doctor` to a bounded count of
  findings plus a "+N more — run `/lcm doctor` for the full scan" tail. The
  v0.2.0 implementation issue owns the exact cap; it is not optional.
- **Slash commands are unchanged and keep the write paths.** `/lcm status`,
  `/lcm doctor`, `/lcm doctor apply`, `/lcm purge`, `/lcm reconcile` all stay.
  Only read-only *visibility* is added for the model; no operator capability
  moves or is removed. The write paths (`doctor apply`, `purge`, `reconcile`)
  remain slash-only and owner-gated.
- **The well-formedness test auto-extends.** `tests/tools/test_schemas_wellformed.py`
  is parametrized over the registry, so the two new schemas are covered for
  free; the implementation issue adds behaviour tests for the two handlers
  (capped output, read-only, correct delegation to the command body).
- **Tool count moves from 8 → 10** in the model-facing surface. ARCHITECTURE.md
  / README "8 agent tools" wording is updated by the v0.2.0 implementation
  issue, not here (this is a decision record).
- **Invariant:** the tool handlers add **no** diagnostic logic of their own.
  They delegate to the existing command bodies. If status/doctor logic
  changes, it changes in one place (`commands/`), and both the slash and tool
  surfaces inherit it.
- **Invariant:** `lcm_status` and `lcm_doctor` stay read-only. If a future need
  arises for a model-callable *write* (e.g. model-triggered `doctor apply`),
  that is a separate ADR with its own owner-gate analysis — it does not ride
  in on this one.

## Open questions / 5% uncertainty

1. **Exact output cap.** The decision mandates capping the tool-variant output
   but does not fix the byte/token budget. The v0.2.0 implementation issue
   sets it — proposed starting point: target ≤ ~1.5k tokens for `lcm_status`
   and ≤ ~20 findings for `lcm_doctor`, tuned against a real tool-result
   budget once Epic-04 compaction interaction is observed.
2. **Should the tool variant signal severity to the model?** A status snapshot
   that includes "circuit breaker OPEN" is more actionable if the tool result
   leads with a one-line health verdict (`healthy` / `degraded` / `failing`)
   the model can branch on cheaply. Recommended but deferred to the
   implementation issue; not load-bearing for this decision.
3. **Description prose is model-facing and tuning-sensitive.** Per ADR-016,
   tool-schema `description` text drives tool-selection behaviour. The two new
   descriptions must make clear these are *read-only self-diagnosis* tools so
   the model reaches for them after a recall miss, not at random. The exact
   prose is owned by the implementation issue and should be reviewed the same
   way the other eight `lcm_*` descriptions were.
4. **Does `run_scan` ever do non-trivial work?** `commands/doctor.py::run_scan`
   is the read-only arm, but a full integrity scan over a large `lcm.db` is not
   free. If the scan is slow on big DBs, the tool variant may need a
   bounded/sampled scan mode distinct from the operator's full scan. Flag for
   the implementation issue; not expected to block v0.2.0.
