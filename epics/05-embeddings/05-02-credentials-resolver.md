---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] voyage: three-tier credential resolver per ADR-022'
labels: 'port, embeddings, voyage, credentials'
---

## Source (TypeScript)
- File: `lossless-claw/src/voyage/client.ts` (resolution is partial: only `process.env.VOYAGE_API_KEY` is implemented, lines 410-419; the `~/.openclaw/credentials/voyage-api-key` file path is documented but not coded — see `client.ts:128-129`).
- Lines: ~10 in TS; the full three-tier resolver is greenfield Python driven by ADR-022.
- Function(s)/class(es): the resolver is **not in the TS source as a single function** — TS reads env at the call site. This issue implements the explicit three-tier resolver that the ADR specifies.

Greenfield Python; ADR-022 is the spec.

## Target (Python)
- File: `src/lossless_hermes/voyage/credentials.py`
- Estimated LOC: ~60 (helper + raise + unit tests are ~150 LOC total)

## Dependencies
- Depends on: #05-01 (`VoyageAuthError` / `VoyageError(kind="auth")` shape so the resolver can raise the same error type the client uses).
- Blocks: engine init (Epic 02 calls this at construction time to pass the resolved key into `VoyageClient(api_key=...)`); #05-07 (backfill); #05-09 (hybrid search) — both transitively need an authenticated client.

## Acceptance criteria

- [ ] Function signature: `resolve_voyage_api_key(config: LosslessHermesConfig, *, env: Mapping[str, str] | None = None, hermes_home: Path | None = None) -> str`. Raises `VoyageError(kind="auth", ...)` if no tier yields a non-empty value.
- [ ] **Resolution order (strict; first non-empty wins):**
  1. `config.voyage_api_key` if set and non-empty after `.strip()`.
  2. `(env or os.environ).get("VOYAGE_API_KEY", "").strip()` if non-empty.
  3. Read `($HERMES_HOME or ~/.hermes) / "lossless-hermes" / "credentials" / "voyage-api-key"` — `Path.read_text().strip()` if the file exists and is non-empty.
- [ ] **Missing-everything error message:** `"voyage_auth: no API key found in config.lossless_hermes.voyage_api_key, $VOYAGE_API_KEY, or $HERMES_HOME/lossless-hermes/credentials/voyage-api-key"`. Matches LCM `client.ts` auth-error formatting style.
- [ ] **Empty-string fall-through:** an empty `config.voyage_api_key` falls through to env; a whitespace-only env value falls through to file. Test fixture for each tier.
- [ ] **`${VOYAGE_API_KEY}` interpolation in `config.voyage_api_key`** works through the standard Hermes config loader (this is the operator-facing path for "checkable-into-git YAML with secret-from-env"). Verify via a fixture that constructs the config with the interpolated string already resolved (the loader handles substitution upstream — the resolver receives a plain string).
- [ ] **No logging of the resolved key.** The key is loaded once and stored on `VoyageClient`. No `log.info(f"loaded key: {key[:4]}…")`-style logging at any level.
- [ ] **Resolver runs at `LCMEngine.__init__`** (engine construction time). The resolved key is passed to `VoyageClient(api_key=...)`. If embeddings are disabled (no Voyage usage configured anywhere), the resolver is bypassed.
- [ ] `mypy --strict src/lossless_hermes/voyage/credentials.py` and `ty check` pass.
- [ ] PR description references ADR-022 explicitly and notes any deviations.

## Tests (`tests/voyage/test_credentials.py`)

Port the test cases in ADR-022 §Consequences:

1. Config inline wins over env+file.
2. Env wins over file (no config).
3. File wins when neither config nor env set.
4. Empty config string falls through to env.
5. Whitespace-only env falls through to file.
6. All three empty → structured `VoyageError(kind="auth")` with the documented message.
7. `${VOYAGE_API_KEY}` interpolation in config works (verified via Hermes config-loader test — may live in a shared fixture).
8. File doesn't exist → tier 3 is skipped silently (not an error; fall through to "missing-everything").
9. File exists but is empty after `.strip()` → falls through to "missing-everything".
10. `hermes_home` override works (custom path → custom credentials file).

## Estimated effort
2–3 hours

## Confidence
95% — ADR-022 fully specifies the resolution order, error message, and test cases. The resolver is ~20 LOC of Python. Residual 5%:
- Windows `$HERMES_HOME` default — `pathlib.Path.home()` is cross-platform but Hermes's Windows install layout isn't verified locally (per ADR-022 §"Open questions"). Linux/macOS beta target only.
- OS keyring tier — explicitly out of scope per ADR-022 §"Open questions"; post-launch addition as tier-0 (strict prefix).

## Files to read before starting
- `docs/adr/022-voyage-credentials.md` (entire — 109 LOC; the spec)
- `docs/porting-guides/embeddings.md` §"API key resolution" (lines 138-143) — original TS reference
- TS source: `lossless-claw/src/voyage/client.ts:128-129, :411` (env-only resolution as it stands)
