# ADR-034: Plugin distribution — directory-mode primary

**Status:** Accepted
**Date:** 2026-05-19
**Confidence:** 95%
**Supersedes:** ADR-001
**Superseded by:** —

> **Implementation is v0.2.0-tracked.** This ADR records the *decision* reached by
> the `hermes-lcm` architecture review at ≥95% confidence. The packaging changes
> (shipping `plugin.yaml` at the package root, a clone-install path /
> `scripts/install.sh`) are scheduled for v0.2.0 — see
> [GitHub issue #134](https://github.com/electricsheephq/lossless-hermes/issues/134).
> v0.1.0's pip/entry-point install path continues to work.

## Context

ADR-001 chose **entry-point mode** as `lossless-hermes`'s sole distribution model:
`pip install lossless-hermes` registers a `[project.entry-points."hermes_agent.plugins"]`
entry point that Hermes loads at startup. ADR-001 rejected directory mode (dropping a
plugin directory under `~/.hermes/plugins/<name>/`) on a specific, load-bearing
premise:

> **Context-engine directory plugins cannot register hooks.**
> `_EngineCollector.register_hook` is an explicit no-op
> (`plugins/context_engine/__init__.py:212-213`, confirmed in spike 002 step 7).
> This kills `pre_llm_call` and `post_llm_call` registration — both load-bearing for
> LCM's `ingest()` and `assemble()`.
> — ADR-001 §"Option B" cons

ADR-001 concluded that a directory plugin gets only the narrow `_EngineCollector`
surface (`register_context_engine` works; `register_hook` / `register_command` do
not), so the 25-subcommand `/lcm` UX and the two lifecycle hooks would be unreachable
in directory mode. Therefore: ship entry-point only; "dropping plugin files into
`~/.hermes/plugins/` is no longer a supported install path."

The `hermes-lcm` architecture review re-examined that premise against the Hermes
codebase and against `stephenschoettler/hermes-lcm` (~540★, a Hermes-LCM plugin
shipping in production today as a **directory plugin**). The review found ADR-001's
premise **verified false** (95% confidence). Two facts:

### Fact 1 — entry-point plugins are CLI-invisible to `hermes plugins`

The Hermes `plugins` CLI (`hermes plugins list` / `install` / `enable`) operates on
**directory plugins** under `~/.hermes/plugins/`. Its discovery component —
`_EngineCollector` and the directory scanner behind the `plugins` subcommand — only
ever scans the **repo-bundled plugin directory and `~/.hermes/plugins/`**. It does
**not** enumerate `importlib.metadata` entry points. (ADR-001 itself notes the entry
points are loaded by a *separate* loop, `hermes_cli/plugins.py:1039-1063`, distinct
from the directory path — but ADR-001 treated that as a benign implementation detail
rather than recognizing its consequence.)

The consequence: a pip-installed, entry-point-only plugin **does not appear in
`hermes plugins list`, cannot be `hermes plugins install`ed, and cannot be
`hermes plugins enable`d.** It is invisible to the CLI surface Hermes operators use
to discover and manage plugins. An operator running `hermes plugins list` to check
whether LCM is installed would see nothing — even with the package pip-installed and
working.

### Fact 2 — a directory plugin DOES get a full `PluginContext` (hooks + commands work)

ADR-001's central claim — that directory mode strips hooks because
`_EngineCollector.register_hook` is a no-op — **conflates two different things**:

- `_EngineCollector` is the narrow collector used specifically for the
  *context-engine slot* of a directory plugin. Its `register_hook` is indeed a no-op
  (`plugins/context_engine/__init__.py:212-213`).
- But that is **not the only thing a directory plugin's `register(ctx)` receives.**
  A directory plugin loaded through the normal Hermes directory-plugin path is
  handed a **full `PluginContext`** — the same `PluginContext` an entry-point plugin
  gets, with working `register_hook`, `register_command`, `register_cli_command`,
  and `register_context_engine`. The directory plugin's hooks and slash commands
  work.

The review's finding: ADR-001 took the no-op behavior of `_EngineCollector` — the
*context-engine sub-collector* — and wrongly generalized it to "directory plugins
cannot register hooks at all." A directory plugin can register hooks and commands;
it gets a full `PluginContext`. The `_EngineCollector` no-op only constrains how the
*engine slot itself* is collected, not the plugin's hook/command surface.

`stephenschoettler/hermes-lcm` is the existence proof: it is a directory plugin, it
registers lifecycle hooks and a slash command, it ships in production, and it has
540★. If ADR-001's premise were true, `hermes-lcm` could not function.

### The resulting situation

ADR-001's decision is therefore inverted by the facts:

- ADR-001 said entry-point mode is the **only** channel with the full surface.
  False — a directory plugin gets a full `PluginContext` too.
- ADR-001 said directory mode loses hooks and the `/lcm` commands. False — those
  work for a directory plugin.
- ADR-001's entry-point-only plugin is **CLI-invisible**: `hermes plugins
  list/install/enable` cannot see it. `hermes-lcm` (directory mode) *is* visible and
  manageable through that CLI.

The constraint forcing a re-decision: which distribution model is **primary** for
v0.2.0 — the entry-point/pip model ADR-001 chose (CLI-invisible), or the directory
model that `hermes-lcm` and the Hermes `plugins` CLI both expect?

## Options considered

### Option A: Keep ADR-001 — entry-point/pip only

- **Description:** Hold ADR-001's decision. Ship only
  `[project.entry-points."hermes_agent.plugins"]`; `pip install lossless-hermes` is
  the sole install path. No `plugin.yaml`, no directory install.
- **Pros:**
  - No change; no v0.2.0 packaging work.
  - Pip handles versioning, dependency resolution (`httpx`, `sqlite-vec`,
    `pydantic`, `pyyaml`, `tenacity`), and uninstall.
- **Cons:**
  - **The plugin is invisible to `hermes plugins list/install/enable`.** Operators
    cannot discover or manage it through the standard Hermes plugin CLI. This is the
    primary operator-facing surface for plugins; being absent from it is a
    real usability defect.
  - **It does not match `hermes-lcm`.** The reference production Hermes-LCM plugin
    is a directory plugin. Operators familiar with `hermes-lcm`'s install flow find
    `lossless-hermes` works differently for no good reason.
  - **It does not match the Hermes `plugins` CLI's mental model.** The CLI is built
    around `~/.hermes/plugins/`; an entry-point plugin sits outside that model.
  - **ADR-001's stated justification is false.** ADR-001 chose entry-point mode
    *because* it believed directory mode loses hooks/commands. That belief is
    verified wrong; the decision built on it cannot stand on its original rationale.
- **Evidence cited:** ADR-001 §Decision, §Consequences; `hermes-lcm` architecture
  review (95% — ADR-001's premise verified false).

### Option B: Directory-mode primary; entry-point kept as a secondary, CLI-invisible pip path

- **Description:**
  - **Directory mode becomes the primary distribution model.** Ship a `plugin.yaml`
    at the **package root**. The install target is `~/.hermes/plugins/lossless-hermes/`
    — the directory the Hermes `plugins` CLI scans. Provide a **clone-install path**
    (`git clone` into `~/.hermes/plugins/lossless-hermes/`) and a `scripts/install.sh`
    that performs that install (clone/copy + dependency install). This matches
    `hermes-lcm` and the `hermes plugins list/install/enable` CLI.
  - **Entry-point mode is kept as a *secondary* path.** `[project.entry-points."hermes_agent.plugins"]`
    stays in `pyproject.toml` so `pip install lossless-hermes` continues to work for
    operators who specifically want a pip-managed install. It is **documented as
    CLI-invisible**: a pip install will *not* show up in `hermes plugins list`.
  - A directory plugin's `register(ctx)` receives a full `PluginContext`, so the
    engine slot, both lifecycle hooks, and the `/lcm` command all register correctly
    in directory mode (Fact 2).
- **Pros:**
  - **The plugin is visible and manageable via `hermes plugins list/install/enable`.**
    Directory install puts it where the CLI looks. This is the standard operator
    surface.
  - **It matches `hermes-lcm`** — the 540★ reference Hermes-LCM plugin — and the
    Hermes `plugins` CLI's directory-centric model. Operators get a consistent
    experience.
  - **Hooks and commands work** — directory plugins get a full `PluginContext`
    (Fact 2). The `_EngineCollector` no-op does not constrain the hook/command
    surface; it only governs how the engine sub-slot is collected.
  - **Pip is not lost** — operators who want a pip-managed install keep it, with the
    CLI-invisibility caveat documented honestly.
  - **In-tree development is natural** — point Hermes at a working tree under
    `~/.hermes/plugins/`, exactly how `hermes-lcm` is developed.
- **Cons:**
  - **Dependency management shifts to the install script.** A directory plugin has
    no pip dependency chain; `scripts/install.sh` (or the documented manual flow)
    must install `httpx`, `sqlite-vec`, `pydantic`, `pyyaml`, `tenacity` into the
    Hermes environment. Mitigation: `scripts/install.sh` runs the dependency install
    explicitly; the pinned set is already known (ADR-006, `docs/reference/dependencies.md`).
  - **Two install paths to document** (directory primary, pip secondary).
    Mitigation: directory is the documented default; pip is a clearly-labeled
    secondary path with the CLI-invisibility caveat. This is honest, and the pip
    path is genuinely useful for some operators.
  - **`plugin.yaml` becomes authoritative** (ADR-001 had said it "is not
    authoritative"). Mitigation: that earlier statement was a consequence of the
    entry-point-only choice; under directory-primary, `plugin.yaml` *is* the plugin
    manifest, which is correct and matches `hermes-lcm`.
- **Evidence cited:** `hermes-lcm` architecture review (95%); `docs/related-work.md`
  (`hermes-lcm` is a shipping directory-mode Hermes-LCM plugin); Hermes `plugins`
  CLI directory-scan behavior; Fact 2 (directory plugin receives a full
  `PluginContext`).

### Option C: Directory-mode primary; drop the entry-point path entirely

- **Description:** Same as Option B, but delete `[project.entry-points."hermes_agent.plugins"]`
  from `pyproject.toml` — directory mode only.
- **Pros:** One install path; no CLI-invisible secondary path to caveat.
- **Cons:**
  - **Removes a working install path with real users.** v0.1.0 shipped pip/entry-
    point as the *only* path. Deleting it in v0.2.0 strands any operator who pip-
    installed v0.1.0 and scripted around it. A *secondary, documented* pip path
    (Option B) costs almost nothing — the entry-point line is a few lines of
    `pyproject.toml` — and preserves that path.
  - **No upside over Option B** beyond avoiding one documentation caveat, which is a
    small price for keeping a working path.
- **Evidence cited:** same as Option B; the cost of removing a working path mirrors
  the "don't strand existing users" reasoning in ADR-030 (sequencing releases so
  existing users are not regressed).

## Decision

Chosen: **Option B — directory-mode install becomes the primary distribution model.
Entry-point/pip is kept as a secondary path, documented as CLI-invisible.**

For v0.2.0:

- Ship a **`plugin.yaml` at the package root** — the directory-plugin manifest.
- The primary install target is **`~/.hermes/plugins/lossless-hermes/`** — the
  directory the Hermes `plugins` CLI scans, matching `stephenschoettler/hermes-lcm`.
- Provide a **clone-install path** (`git clone` into `~/.hermes/plugins/lossless-hermes/`)
  and a **`scripts/install.sh`** that automates clone/copy + dependency install.
- **Keep `[project.entry-points."hermes_agent.plugins"]`** in `pyproject.toml` as a
  secondary pip path, **documented as CLI-invisible** (`hermes plugins list` will
  not show a pip-installed copy).

ADR-001 is superseded. Its premise — "context-engine directory plugins cannot
register hooks" — is verified false: a directory plugin's `register(ctx)` receives a
full `PluginContext` (hooks and commands work). ADR-001's entry-point-only choice
left the plugin invisible to `hermes plugins list/install/enable`.

## Rationale

1. **ADR-001's load-bearing premise is verified false.** ADR-001 chose entry-point
   mode *specifically because* it believed directory mode loses hooks and the `/lcm`
   commands. The review established (95% confidence) that a directory plugin gets a
   full `PluginContext` — `register_hook`, `register_command`,
   `register_cli_command`, `register_context_engine` all work. The
   `_EngineCollector.register_hook` no-op (`plugins/context_engine/__init__.py:212-213`)
   that ADR-001 cited is the behavior of the *context-engine sub-collector*, not the
   plugin's overall hook/command surface. ADR-001 over-generalized it. A decision
   whose sole justification is a false premise cannot stand.

2. **The reference production Hermes-LCM plugin is a directory plugin.**
   `stephenschoettler/hermes-lcm` (~540★) ships in production as a directory plugin
   with lifecycle hooks and a slash command. It is the existence proof that Fact 2
   is true — if directory plugins could not register hooks, `hermes-lcm` could not
   work. Matching its distribution model gives operators a consistent experience and
   removes a needless divergence.

3. **An entry-point-only plugin is invisible to `hermes plugins`.** The Hermes
   `plugins` CLI (`list` / `install` / `enable`) is the standard operator surface
   for plugin discovery and management; it scans `~/.hermes/plugins/` (and the
   repo-bundled dir), not `importlib.metadata` entry points. ADR-001's plugin does
   not appear there. Directory-mode install puts `lossless-hermes` where the CLI
   looks, so `hermes plugins list` shows it and `hermes plugins enable` manages it.

4. **Keeping pip as a secondary path costs almost nothing and helps real
   operators.** The `[project.entry-points."hermes_agent.plugins"]` block is a few
   lines of `pyproject.toml`. Some operators — CI, containerized deployments,
   environments that manage everything through pip — genuinely prefer a pip-managed
   install. Option B keeps that path, honestly labeled as CLI-invisible. Option C's
   only gain over B is avoiding one documentation caveat; that does not justify
   stranding pip users (v0.1.0 shipped pip as the *only* path).

5. **`plugin.yaml` at the package root is the correct manifest under directory
   mode.** ADR-001 stated `plugin.yaml` "is not authoritative" — but that was a
   *consequence* of the entry-point-only choice, not an independent fact. Under
   directory-primary, `plugin.yaml` *is* the plugin manifest the Hermes directory
   loader reads, exactly as in `hermes-lcm`. Shipping it at the package root makes
   the same checkout serve as a directory plugin (clone into `~/.hermes/plugins/`)
   and a pip package.

## Consequences

- **ADR-001 is superseded.** Its status line is updated to
  `Superseded by ADR-034`. Its text is preserved unchanged (ADRs are append-only per
  CLAUDE.md).

- **v0.2.0 packaging work (issue #134):**
  - **Add `plugin.yaml` at the package root** — the directory-plugin manifest
    (plugin name, entry module, declared hooks/commands, engine slot), matching the
    shape `hermes-lcm` uses.
  - **Document the directory install as primary:** clone (or copy) the package into
    `~/.hermes/plugins/lossless-hermes/`; the Hermes `plugins` CLI then discovers it.
  - **Add `scripts/install.sh`** — automates the directory install: places the
    package under `~/.hermes/plugins/lossless-hermes/` and installs the pinned
    dependency set (`httpx`, `sqlite-vec`, `pydantic`, `pyyaml`, `tenacity` — per
    ADR-006 / `docs/reference/dependencies.md`) into the Hermes environment.
  - **Keep `[project.entry-points."hermes_agent.plugins"]`** in `pyproject.toml`.
    Document the pip install as a **secondary path that is CLI-invisible** —
    `hermes plugins list` will not show a pip-installed copy. The README quickstart
    leads with the directory install; the pip path is a clearly-labeled alternative.
  - Update the README quickstart (currently entry-point-first per ADR-001) so
    directory install is the primary instruction.

- **`docs/spike-results/002` step 7 is corrected.** Spike 002's step 7
  (`docs/spike-results/002-hermes-pre-llm-call.md` line 21) states that "directory-mode
  context-engine plugins **cannot** register `pre_llm_call` hooks at all." A
  correction note is added to spike 002 stating that this `_EngineCollector` claim
  is **wrong**: a directory plugin's `register(ctx)` receives a full `PluginContext`,
  and hooks/commands work; the `_EngineCollector.register_hook` no-op only governs
  the context-engine sub-collector, not the plugin's hook surface. **Spike 002's
  engine-rewrite analysis stays valid** — its core finding (that `pre_llm_call` is
  additive-only and the `compress()` seam is the message-rewrite mechanism) is
  unaffected by this correction; only the directory-mode-loses-hooks claim in step 7
  is wrong. (Spike 002's `compress()`-rewrite analysis is also the subject of
  ADR-032; the two corrections are independent.)

- **`docs/reference/dependencies.md`** — the section recommending the
  `[project.entry-points."hermes_agent.plugins"]` block as *the* distribution
  shape is updated to present directory mode as primary and the entry-point block as
  the secondary pip path.

- **`plugin.yaml` becomes authoritative.** ADR-001's consequence "`plugin.yaml` is
  not authoritative" is reversed: under directory-primary, `plugin.yaml` is the
  manifest the Hermes directory loader reads.

- **`config.yaml` requirements are unchanged.** Operators still set
  `context.engine: lcm` to select the engine and still add the plugin to the
  `plugins.enabled` allowlist — directory mode does not change opt-in selection
  (ADR-001's `config.yaml` consequences carry over).

- **Heavy init still belongs in `on_session_start`.** ADR-001's invariant — DB open
  and migration-ladder run go in `ContextEngine.on_session_start`, not in
  `register()` — is unchanged and carries over.

- **Invariant — directory install is the primary, supported path.** Documentation,
  the README quickstart, and `scripts/install.sh` treat
  `~/.hermes/plugins/lossless-hermes/` as the canonical install location. The plugin
  must be discoverable by `hermes plugins list`.

- **Invariant — the pip/entry-point path stays functional but is documented as
  CLI-invisible.** `[project.entry-points."hermes_agent.plugins"]` is not removed;
  any doc that mentions the pip install also states that it does not appear in
  `hermes plugins list`.

- **Invariant — the same checkout serves both modes.** Because `plugin.yaml` sits at
  the package root alongside `pyproject.toml`, one checkout works as a directory
  plugin (clone into `~/.hermes/plugins/`) and as a pip package. They must not
  diverge.

- **v0.1.0 ships unchanged.** v0.1.0 is already released with the pip/entry-point
  path; this ADR scopes v0.2.0 packaging work (issue #134). v0.1.0's pip install
  continues to function.

## Open questions / 5% uncertainty

1. **Dependency install in directory mode — how robust does `scripts/install.sh`
   need to be?** A directory plugin has no pip dependency chain, so the install
   script must install the pinned deps into *the same environment Hermes runs in*.
   Detecting that environment reliably (system Python vs a venv vs a Hermes-managed
   environment) is the main unknown. Mitigation: `scripts/install.sh` should detect
   and report the target environment, install the pinned set, and fail loudly if it
   cannot — never silently install into the wrong interpreter. Finalize the
   detection strategy in v0.2.0 design (#134); cross-check how `hermes-lcm`'s
   install handles this.

2. **Exact `plugin.yaml` schema fields Hermes's directory loader requires.** The
   manifest shape (required keys, how the engine slot vs hooks vs commands are
   declared) must match what the Hermes directory-plugin loader expects. Mitigation:
   model `plugin.yaml` on `stephenschoettler/hermes-lcm`'s working manifest and
   validate against a live `hermes plugins list/enable` during v0.2.0.

3. **Does `hermes plugins install` expect a registry / remote source, or only a
   local directory?** If `hermes plugins install` can pull from a git URL or a
   registry, `lossless-hermes` may be installable by name; if it only adopts a
   pre-placed local directory, the clone step is manual. Mitigation: confirm the
   `hermes plugins install` capability surface in v0.2.0; `scripts/install.sh`
   covers the manual clone case regardless.

4. **Could a directory install and a pip install of the same plugin collide?** An
   operator who has both a directory copy under `~/.hermes/plugins/lossless-hermes/`
   *and* a pip-installed entry point could load the plugin twice. Mitigation: the
   plugin's `register()` should be idempotent / detect a double-registration; v0.2.0
   docs warn against installing both ways at once, and `scripts/install.sh` can
   check for a competing pip install and warn.

5. **`_EngineCollector` behavior could change upstream.** The Fact 2 finding (a
   directory plugin gets a full `PluginContext`) reflects Hermes core as reviewed.
   If a future Hermes release changes how directory plugins are loaded, the
   directory path must be re-validated. Mitigation: an integration test pins a
   Hermes version and exercises a directory install end-to-end (engine slot + both
   hooks + `/lcm` command) so a regression is caught — mirroring the ADR-001
   open-question mitigation that pinned a Hermes version against entry-point drift.
