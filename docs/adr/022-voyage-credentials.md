# ADR-022: Voyage credential resolution

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

The Voyage HTTP client needs an API key. Three constituencies have different needs:

- **Existing OpenClaw users migrating to Hermes.** They have a credential file at `~/.openclaw/credentials/voyage-api-key`. Asking them to set an env var or hand-edit a YAML is friction.
- **CI / containerized deployments.** Env var is the canonical mechanism; mounting a secrets file works but env wins for portability.
- **Operators who hand-edit config files.** YAML inline (with optional `${VOYAGE_API_KEY}` interpolation) is the readable path.

The TS source (`client.ts:128-129, :411`) resolves in this order:
1. Explicit `apiKey` option (tests, override).
2. `process.env.VOYAGE_API_KEY`.
3. (Documented only — not implemented in the constructor) `~/.openclaw/credentials/voyage-api-key`.

The Python port lands in a Hermes runtime where YAML config (`~/.hermes/config.yaml`) is the operator-facing knob, env vars are the CI knob, and file paths under `$HERMES_HOME` are the migration path. The question: what order, and what file path?

## Options considered

### Option A: Three-tier resolution — config inline > env > file

- Description: resolve in this order:
  1. `context.lossless_hermes.voyage_api_key` from `config.yaml` (string; supports `${VOYAGE_API_KEY}` interpolation per the standard Hermes config-loader path).
  2. `os.environ["VOYAGE_API_KEY"]`.
  3. File contents at `$HERMES_HOME/lossless-hermes/credentials/voyage-api-key` (default `$HERMES_HOME = ~/.hermes`).
  Strip whitespace and check non-empty at each tier; first non-empty wins.
- Pros:
  - Inline config supports operators who want everything in one YAML and who use Hermes's existing `${VAR}` interpolation for non-secret substitution patterns.
  - Env var matches LCM's documented variable name (`VOYAGE_API_KEY`) and CI conventions.
  - File path mirrors the OpenClaw layout (`~/.openclaw/credentials/voyage-api-key` → `~/.hermes/lossless-hermes/credentials/voyage-api-key`), reducing migration friction for the install base.
  - Each tier has a clear operator narrative: inline for "I keep config in git with env interpolation"; env for CI; file for "I migrated from OpenClaw or I run secret-mounting infra."
- Cons:
  - Three sources is one more than the TS does in code (TS treats #3 as documented-only). Mitigated: the resolver function is ~20 LOC and well-tested.
  - File-path tier loads on every `VoyageClient()` construction. Mitigated: read once, cache; or detect at engine init time and pass to constructor.
- Evidence cited:
  - `dependencies.md` / `embeddings.md` (per user brief): three-tier resolution matches the OpenClaw legacy and Hermes conventions.
  - `embeddings.md` "ADR-?: Where does `VOYAGE_API_KEY` come from?" lists options (A) env-only, (B) env + file, (C) keyring. Three-tier (config inline + env + file) is a strict superset of (B) and supports the migration path naturally.
  - LCM `client.ts:128-129` documents the file path; we make it real.

### Option B: Env var only

- Description: only `os.environ["VOYAGE_API_KEY"]`. Empty → error.
- Pros: simplest. Single source.
- Cons:
  - Breaks the OpenClaw-migration path. Existing users would need to convert their credential file to an env var.
  - No inline-config option for operators who prefer everything in YAML.

### Option C: File only, like the OpenClaw layout

- Description: read from `$HERMES_HOME/lossless-hermes/credentials/voyage-api-key`. Mirror the OpenClaw layout exactly.
- Pros: matches existing OpenClaw conventions perfectly.
- Cons:
  - Breaks CI conventions (env-var-secrets are standard in GHA, GitLab CI, etc.).
  - Forces every operator to maintain a file even if they prefer config-inline.

### Option D: OS keyring (e.g., Keychain, GNOME Keyring)

- Description: store the key in the OS keyring, fetch via `keyring` Python package.
- Pros: native OS-level secret management.
- Cons:
  - Doesn't work in headless environments without a keyring backend.
  - Hermes doesn't currently use the keyring for any other secret; adding it for one key is a foot-gun.
  - Embeddings.md notes "Recommend (A) for v1, layer in (B) post-launch" — keyring is explicitly post-launch territory.

## Decision

Chosen: **Option A (three-tier resolution: config inline > env > file)**.

## Rationale

- The three-tier resolution covers all three constituencies. Inline config for YAML-first operators (with `${VAR}` interpolation for the secret-management workflow). Env for CI and 12-factor deployments. File for OpenClaw-migrating users.
- File path `$HERMES_HOME/lossless-hermes/credentials/voyage-api-key` mirrors the OpenClaw layout one-for-one. An OpenClaw operator can `cp ~/.openclaw/credentials/voyage-api-key ~/.hermes/lossless-hermes/credentials/voyage-api-key` and be done. We could symlink-check both paths in v1 for the smoothest migration, but the brief specifies the new path.
- Env var name `VOYAGE_API_KEY` matches LCM's documented variable. Doesn't surprise upstream operators.
- Config-inline supports `${VOYAGE_API_KEY}` interpolation through the standard Hermes config-loader. An operator who wants their YAML to be checkable into git can write `voyage_api_key: "${VOYAGE_API_KEY}"` and keep the secret out of the file.

The resolver is ~20 LOC, has obvious test cases (each tier wins when populated; empty strings don't count; whitespace stripped; missing-everything raises a structured error), and runs once at engine init.

## Consequences

- New helper: `src/lossless_hermes/voyage/credentials.py` — `resolve_voyage_api_key(config: LosslessHermesConfig, *, env: Mapping[str, str] | None = None, hermes_home: Path | None = None) -> str`. Raises `VoyageAuthError` if no tier yields a non-empty value.
- Resolution order (strict):
  1. `config.voyage_api_key` if set and non-empty after strip.
  2. `(env or os.environ).get("VOYAGE_API_KEY", "").strip()` if non-empty.
  3. `($HERMES_HOME or ~/.hermes) / "lossless-hermes" / "credentials" / "voyage-api-key"` — read text, strip, if non-empty.
- The resolver runs at `LCMEngine.__init__` (engine construction time). The resolved key is passed to `VoyageClient(api_key=...)`. If embeddings are disabled (no Voyage usage configured), the resolver is bypassed.
- Missing-key behavior: a clear `VoyageAuthError("voyage_auth: no API key found in config.lossless_hermes.voyage_api_key, $VOYAGE_API_KEY, or $HERMES_HOME/lossless-hermes/credentials/voyage-api-key")` — operator sees actionable instructions. Matches LCM's existing auth-error formatting style.
- The credentials directory has restrictive permissions documented in operator setup: `chmod 700 $HERMES_HOME/lossless-hermes/credentials/`, `chmod 600 voyage-api-key`. The plugin does NOT enforce permissions (out of scope), but documentation guides operators.
- Migration path: a Phase-2 helper `lcm credentials import-openclaw` command can copy `~/.openclaw/credentials/voyage-api-key` into the new location if both files exist and the destination is missing. Not in scope for this ADR; flag for the operator-ops epic.
- Tests:
  - Config inline wins over env+file.
  - Env wins over file (no config).
  - File wins when neither config nor env set.
  - Empty config string falls through to env.
  - Whitespace-only env falls through to file.
  - All three empty → structured `VoyageAuthError`.
  - `${VOYAGE_API_KEY}` interpolation in config works (verified via Hermes config-loader test).

## Open questions / 5% uncertainty

- **`$HERMES_HOME` default on Windows.** Hermes's Windows install path may differ. The default `~/.hermes` works via `pathlib.Path.home()` cross-platform. Verify on a Windows test before launch (not currently in scope for the Linux/macOS-only beta).
- **OS keyring integration.** Embeddings.md flags (C) as post-launch. If a future operator constituency demands it (e.g., enterprise customers with mandated keyring use), add a tier-0 (`keyring.get_password("lossless-hermes", "voyage")` if available). Doesn't require rewriting this ADR; tier-0 is a strict prefix.
- **Symlink to old OpenClaw path.** For zero-friction migration, v1 could check `~/.openclaw/credentials/voyage-api-key` as a tier-4 fallback. Not specified in the brief; defer to operator feedback.
- **Secrets in audit logs.** The credential is loaded once and stored on the `VoyageClient`. Never logged. The `summarizeBody` PII-suppression in the Voyage client (Wave-4/7 fix) already redacts request bodies; the key itself is in the `Authorization` header and isn't echoed back.
