---
name: Port issue
about: A single discrete unit of porting work
title: '[epic-05] concurrency: port worker-lock.ts → concurrency/worker_lock.py'
labels: 'port, embeddings, concurrency, lock'
---

## Source (TypeScript)
- File: `lossless-claw/src/concurrency/worker-lock.ts`
- Lines: 215
- Function(s)/class(es): `acquireLock` (69-109 — GC stale + INSERT OR IGNORE), `heartbeatLock` (139-168 — UPDATE with `expires_at > now` Wave-1 guard), `releaseLock` (121-130), `lockInfo` (174-202 — diagnostic for `/lcm health`), `generateWorkerId` (210-215).

## Target (Python)
- File: `src/lossless_hermes/concurrency/worker_lock.py`
- Estimated LOC: ~220

## Dependencies
- Depends on: Epic 01 (the `lcm_worker_lock` table must be defined in the migration with the exact schema below). #05-05 implicitly via the `concurrency/model.py` peer module that defines `WorkerJobKind` and the TTL constants.
- Blocks: #05-07 (backfill cron acquires + heartbeats); the lock is used by every cross-process job kind.

## Acceptance criteria

- [ ] **`lcm_worker_lock` schema** (defined in Epic 01's migration; this issue verifies it matches `worker-lock.ts`):
  ```sql
  CREATE TABLE lcm_worker_lock (
      job_kind TEXT PRIMARY KEY,         -- one row per kind; PK uniqueness IS the lock
      worker_id TEXT NOT NULL,           -- "<role>-<pid>-<startMs>-<nonce>"
      acquired_at TEXT NOT NULL,         -- ISO-8601 (datetime('now'))
      expires_at TEXT NOT NULL,          -- ISO-8601; lexicographic compare is safe
      last_heartbeat_at TEXT NOT NULL,
      job_session_key TEXT,              -- informational scope
      job_metadata TEXT                  -- diagnostic tag
  );
  ```
  ISO-8601 strings via SQL `datetime('now')` (server-side clock — avoids Python-vs-SQL clock skew per ADR-018 §"Cross-process clock skew").
- [ ] **`acquire_lock(db, job_kind, *, worker_id, ttl_ms=WORKER_LOCK_TTL_MS, job_session_key=None, job_metadata=None) -> bool`** (port of `worker-lock.ts:69-109`):
  1. **GC step:** `DELETE FROM lcm_worker_lock WHERE job_kind = ? AND expires_at <= datetime('now')`. The `<=` (not `<`) is intentional — `ttl=0` is immediately reclaimable.
  2. **`INSERT OR IGNORE`:** `(job_kind, worker_id, acquired_at, expires_at, last_heartbeat_at, job_session_key, job_metadata) VALUES (?, ?, datetime('now'), datetime('now', '+' || ? || ' seconds'), datetime('now'), ?, ?)`.
  3. `db.commit()` (Python sqlite3 autocommit-off quirk — porting guide §"Python sqlite3 gotcha" warns against forgetting).
  4. Return `cur.rowcount > 0`.
  5. Raise `ValueError` on empty/whitespace `worker_id`.
- [ ] **`heartbeat_lock(db, job_kind, worker_id, *, ttl_ms=WORKER_LOCK_TTL_MS) -> bool`** (port of `worker-lock.ts:139-168`):
  ```sql
  UPDATE lcm_worker_lock
  SET last_heartbeat_at = datetime('now'),
      expires_at = datetime('now', '+' || ? || ' seconds')
  WHERE job_kind = ? AND worker_id = ? AND expires_at > datetime('now')
  ```
  - **Wave-1 fix** (load-bearing): the `expires_at > datetime('now')` predicate prevents silent re-extension of an already-expired lock after another worker GC'd + acquired. Carry inline `# LCM Wave-1 (2025-11-XX): expires_at > now predicate prevents silent re-extension after stale GC + reacquire` per ADR-029. (Note: this is a separate Wave-1 fix from the Voyage `BACKOFF_CAP_MS=25_000` one — same wave, different file.)
  - Return `cur.rowcount > 0`. Caller MUST abort the tick when this returns False (the lock was lost).
- [ ] **`release_lock(db, job_kind, worker_id) -> bool`** (port of `worker-lock.ts:121-130`):
  `DELETE FROM lcm_worker_lock WHERE job_kind = ? AND worker_id = ?`. Worker-id check prevents releasing someone else's lock. Returns `cur.rowcount > 0`.
- [ ] **`lock_info(db, job_kind) -> LockInfo | None`** (port of `worker-lock.ts:174-202`):
  - Dataclass `LockInfo(job_kind, worker_id, acquired_at, expires_at, last_heartbeat_at, job_session_key, job_metadata)`.
  - `SELECT * FROM lcm_worker_lock WHERE job_kind = ?`; return `None` if no row.
  - Used by `/lcm health` (Epic 08) and by tests.
- [ ] **`generate_worker_id(role) -> str`** (port of `worker-lock.ts:210-215`):
  ```python
  def generate_worker_id(role: str) -> str:
      return f"{role}-{os.getpid()}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
  ```
  Role examples: `"gateway"`, `"worker"`, `"backfill-autostart"`. The pid + ms-timestamp + 6-hex-nonce makes collisions effectively impossible across processes.
- [ ] **Heartbeat task wrapper** (optional convenience, used by backfill #05-07):
  ```python
  async def run_with_heartbeat(
      db, job_kind, worker_id, *,
      ttl_ms=WORKER_LOCK_TTL_MS,
      heartbeat_ms=WORKER_HEARTBEAT_MS,
      body: Callable[[], Awaitable[T]],
  ) -> T | None:
      """Acquire lock; run `body` with a background heartbeat task; release in finally.
      Returns body's result, or None if acquisition failed."""
      ...
  ```
  Background `asyncio.create_task` sleeps `heartbeat_ms / 1000`, calls `heartbeat_lock`; if it returns False, signals body via an `asyncio.Event` so the body can abort cleanly. Cancellation-safe (try/finally releases lock even on auth re-throw).
- [ ] **Python sqlite3 commit:** **every** INSERT/UPDATE/DELETE must call `db.commit()` (PEP-249 autocommit-off default). Tests verify commits land by opening a second connection and reading the row.
- [ ] `mypy --strict` and `ty check` pass.
- [ ] All 270 LOC of `test/worker-lock.test.ts` + `test/lcm-worker-lock.test.ts` ported to `tests/concurrency/test_worker_lock.py`.

## Tests (`tests/concurrency/test_worker_lock.py`)

Cases from `test/worker-lock.test.ts` (150 LOC) + `test/lcm-worker-lock.test.ts` (120 LOC):

- First `acquire_lock` from worker A returns `True`; second from worker B returns `False`.
- `release_lock(A)` frees the lock; B can now acquire.
- `release_lock` with wrong `worker_id` no-ops; original holder still owns.
- `acquire_lock(worker_id="")` raises `ValueError`.
- `ttl_ms=0` → immediately stale; next `acquire_lock` GC's and succeeds.
- Non-expired lock blocks (`ttl_ms=90_000` then immediate re-acquire from same worker returns False).
- `heartbeat_lock` from holder extends `expires_at` (verify via two `lock_info` reads with a small sleep).
- `heartbeat_lock` from non-holder returns `False` (wrong worker_id).
- **Wave-1 case:** A acquires with `ttl=0.1s`; sleep `0.2s`; B GC's + acquires; A's `heartbeat_lock` now returns `False` (the `expires_at > now` predicate kicks in even though A's worker_id matches a row B just inserted — actually B's row has B's worker_id so A's predicate fails on worker_id mismatch; the more important case is A's row was GC'd by B's acquire, so A's UPDATE matches 0 rows). Verify the exact scenario the porting guide §"Worker lock" lists.
- `job_session_key` and `job_metadata` round-trip via `lock_info`.
- `lock_info` returns `None` when no row exists.
- `generate_worker_id("gateway")` returns a string with the role prefix, the pid, a ms-timestamp, and a 6-hex-char nonce.
- Two different `job_kind`s don't conflict (independent rows).
- `run_with_heartbeat`: heartbeat fires every `heartbeat_ms`; releases on body completion; releases on body exception (try/finally).
- `run_with_heartbeat` with a stolen lock (manually delete the row mid-body) signals the body to abort.

## Estimated effort
4 hours

## Confidence
95% — the SQL ports verbatim (TEXT comparison on ISO-8601 is lexicographic-safe; `datetime('now')` is server-side and avoids clock-skew). ADR-018 pins the design. Residual 5%:
- Cross-process clock skew on machines with badly-set system clocks is unmitigable at the SQL level. Same risk as TS; documented.
- The `INSERT OR IGNORE` race window (another process acquires between DELETE and INSERT) results in our `INSERT` no-oping and the caller being told False. Acceptable — never silently double-acquires. Per ADR-018 §"Cross-process clock skew" open question.

## Files to read before starting
- `docs/porting-guides/embeddings.md` §"Worker lock (cross-process)" (lines 748-894)
- `docs/adr/018-concurrency-model.md` §"Cross-process safety lives in SQL" and §"Cross-process clock skew"
- `docs/adr/029-wave-fix-provenance.md` (Wave-1 inline comment format)
- TS source: `lossless-claw/src/concurrency/worker-lock.ts` (entire — 215 LOC)
- TS source: `lossless-claw/src/concurrency/model.ts` (entire — 147 LOC; constants)
- TS tests: `lossless-claw/test/worker-lock.test.ts` (entire — 150 LOC)
- TS tests: `lossless-claw/test/lcm-worker-lock.test.ts` (entire — 120 LOC)
