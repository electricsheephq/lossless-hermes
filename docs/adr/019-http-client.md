# ADR-019: HTTP client choice

**Status:** Accepted
**Date:** 2026-05-13
**Confidence:** 95%
**Supersedes:** —
**Superseded by:** —

## Context

The Voyage client (`src/voyage/client.ts`, 616 LOC) is the only external HTTP surface in lossless-hermes. It must:
- POST JSON to `/v1/embeddings` and `/v1/rerank`.
- Honor per-attempt timeouts (default 60s) with a clear abort mechanism.
- Parse `Retry-After` headers (numeric seconds or HTTP-date).
- Retry on 5xx and 429-without-Retry-After with exponential backoff.
- Run inside `asyncio.create_task` worker bodies (per ADR-018, ADR-020) without blocking the event loop.
- Survive socks proxies when operators run via `HTTP_PROXY`/`HTTPS_PROXY` envs (Hermes precedent).

Hermes itself already pins `httpx[socks]==0.28.1` in its dependency tree. Adding a second HTTP client to the runtime would split connection pools, double the TLS-cert verification surface, and create version-drift risk.

The TS source uses native `fetch` + `AbortController`. The mapping table at spike-results/004-voyage-python-client.md §"Mapping table" lists every TS primitive's Python equivalent — all map cleanly to `httpx`.

## Options considered

### Option A: `httpx[socks]==0.28.1` (pinned to match Hermes host)

- Description: import `httpx`; use `httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))` as the long-lived client; `await client.post(url, json=body, headers=...)` for requests. Reuse Hermes's existing pin to keep one HTTP library in the runtime.
- Pros:
  - Same pin as the host. No version-drift; Hermes upgrades carry the LCM port along.
  - Native async (`httpx.AsyncClient`) — works with ADR-018's `asyncio.Task` workers.
  - `httpx.Timeout(total, connect)` matches TS `AbortController + setTimeout` cleanly (spike-004 §"Mapping table").
  - Live round-trip in spike-004 succeeded on first attempt (510ms embed, 304ms rerank). All 24 unit fixtures from `test/voyage-client.test.ts` translate to `respx` (httpx mock router).
  - `[socks]` extra adds SOCKS proxy support that Hermes operators rely on.
  - Built-in connection pooling, HTTP/2 (opt-in via `h2`), retries via `httpx.transports.Retry`.
- Cons:
  - One more pinned dep to track. Mitigated by matching the host pin exactly.
- Evidence cited:
  - `spike-results/004-voyage-python-client.md` §"Recommendation": "Pin `httpx>=0.27,<0.30` (`json=` body kwarg, `aclose()` API, and `Timeout` semantics are stable across this range)." User instructed to pin at `0.28.1` to match Hermes.
  - `embeddings.md`: "Python port: `httpx.AsyncClient` for Voyage."

### Option B: `aiohttp`

- Description: use `aiohttp.ClientSession` with `aiohttp.ClientTimeout`.
- Pros: well-established async HTTP library.
- Cons:
  - Different pin than Hermes — adds a second HTTP stack to the runtime image.
  - Slightly different timeout semantics (no separate connect/read split as clean as httpx).
  - Retry-After parsing has to be hand-rolled (no different from httpx, but the mapping table in spike-004 is for httpx).

### Option C: stdlib `urllib.request` / `http.client`

- Description: use Python's stdlib HTTP.
- Pros: zero dependencies.
- Cons:
  - Sync only. Async wrapping via `asyncio.to_thread` works but creates a thread hop for every request — worse than httpx's native async.
  - No connection pooling without manual machinery.
  - `Retry-After` parsing and proxy support are all hand-rolled.
  - Rejected: trades a 1-line pin for 200 LOC of HTTP-library reimplementation.

### Option D: `requests` (sync) + `requests-async`

- Description: use sync `requests` with a thread-executor adapter for asyncio.
- Pros: familiar API.
- Cons:
  - Thread hop per request (same problem as Option C).
  - `requests-async` is unmaintained.
  - Rejected.

## Decision

Chosen: **Option A (`httpx[socks]==0.28.1`)**.

## Rationale

- `dependencies.md` (referenced in user's brief) aligns the port with the host's HTTP stack. Hermes pins `httpx[socks]==0.28.1`; we use the same pin. No second HTTP library enters the runtime.
- spike-004 verified `httpx` end-to-end against the production Voyage API. Every TS primitive in `client.ts` maps to an `httpx` equivalent (spike-004 §"Mapping table"). The 24-fixture test plan translates to `respx` mocks.
- ADR-018's worker loops are `asyncio.Task` bodies; `httpx.AsyncClient` is async-native. No thread hops, no executor plumbing, no event-loop blocking on network I/O.
- For retry helpers: `tenacity` is already in Hermes's dep tree. Per the LCM TS source, the Voyage retry rules are load-bearing across 11 review waves (429-with-lock-budget gate, exponential backoff to a 25s cap, PII suppression on error bodies). We hand-port `postWithRetry` rather than relying on `tenacity` for the inner loop — the per-error-class branching (auth vs bad_request vs rate_limit vs server_error vs network) is too specific for tenacity's predicate API. `tenacity` is available where general-purpose retry helpers (e.g. operator-side health checks) might use it.
- The `[socks]` extra is required by Hermes operators who run via SOCKS proxies. Carrying it through costs nothing.

## Consequences

- `pyproject.toml` adds `httpx[socks]==0.28.1` and `tenacity` (already in Hermes; we restate the pin to avoid surprise if Hermes drops it).
- `src/lossless_hermes/voyage/client.py` imports `httpx`, uses `httpx.AsyncClient` as a long-lived instance on `VoyageClient`. Client is closed in the engine shutdown path.
- Connection-pool defaults (`max_keepalive_connections=20, max_connections=100`) are accepted from httpx defaults but pinned explicitly in the client constructor to avoid implicit drift across httpx point releases (spike-004 risk note §4).
- `httpx.Timeout(60.0, connect=10.0)` per request. Connect timeout 10s prevents indefinite hangs on cold-start; total 60s matches TS `DEFAULT_TIMEOUT_MS=60_000`.
- Per spike-004 risk note §5: real Voyage 429 response shape isn't yet captured in fixtures (would have burned quota). Mitigation: snapshot the first real-staging 429 as a `respx` fixture; until then, the synthetic 429 fixture from `test/voyage-client.test.ts` stands in.
- Operators get socks proxy support automatically. `HTTP_PROXY` / `HTTPS_PROXY` envs work; corporate proxies that require SOCKS5 work.
- Precludes switching to a custom HTTP stack later without re-doing the retry porting. Acceptable.
- Concrete pin (`==0.28.1` vs `~=0.28.0`): exact pin avoids "works on dev box, breaks on CI" drift. If Hermes upgrades, we bump both repos in lockstep with a tested change.

## Open questions / 5% uncertainty

- **httpx breaking changes across 0.28 → 0.30.** spike-004 confirmed `json=`, `aclose()`, and `Timeout` semantics are stable. If a future Hermes upgrade past 0.30 changes any of these, the port needs revisiting. Mitigation: pin exactly to track the host.
- **HTTP/2 not enabled by default.** httpx supports HTTP/2 via the `h2` extra. Spike-004 confirmed Voyage doesn't require HTTP/2; defaulting to HTTP/1.1 matches TS `fetch` behavior. If Voyage's CDN ever requires HTTP/2 (unlikely), opt in then.
- **Connection-pool semantics under burst load.** `max_connections=100` is generous for one process, but if a future deployment runs many parallel embedding-backfill workers, the pool could throttle. Mitigation: revisit when concurrent worker count crosses single digits.
- **Tenacity for retry helpers vs hand-rolled.** Voyage's retry loop is hand-rolled per the TS source's load-bearing rules. Tenacity is available for cases where simple "retry up to N times with backoff" suffices (e.g. an operator-side health probe). Don't try to retrofit tenacity into the Voyage client.
