"""LCM SQLite connection factory — the single sanctioned open/close path.

Ports ``lossless-claw/src/db/connection.ts`` (commit ``1f07fbd``, ~170 LOC) to
Python. This module is the **only** place that opens an ``lcm.db`` connection
per ADR-004 §Consequences ("``open_lcm_db()`` is the only sanctioned
connection factory") — every other module receives an already-configured
connection rather than calling :func:`sqlite3.connect` directly.

### What it does

1. Resolves and normalizes the input path (in-memory vs file-backed), creating
   parent directories for file-backed DBs (``mkdir -p`` semantics).
2. Opens a connection using the chosen driver — stdlib ``sqlite3`` by default,
   ``apsw`` if ``driver="apsw"`` (per ADR-004 the apsw extra is opt-in).
3. Enables loadable extensions, loads ``sqlite-vec`` (per spike-001 §"Load
   pattern"), then disables loadable extensions again to tighten the attack
   surface.
4. Applies the seven PRAGMAs documented in ``docs/porting-guides/storage.md``
   §3 — in the **exact same order** as the TS ``configureConnection()``:
   journal_mode → busy_timeout → foreign_keys (+ assertion) → cache_size →
   synchronous → temp_store. The WAL pragma uses the
   :func:`_apply_wal_with_fallback` helper so the connection still opens on
   NFS/SMB/FUSE filesystems where WAL is unsupported (storage.md §10.4 —
   mirrors ``hermes-agent/hermes_state.py:128`` ``apply_wal_with_fallback``).
5. Tracks the connection in a module-level registry keyed by
   ``(normalized_path, thread_id)`` so test fixtures can call
   :func:`close_lcm_connection` to tear down all per-thread connections for a
   path (ADR-007 §Recommendation).
6. On close, runs ``PRAGMA optimize`` best-effort, then closes the underlying
   connection (``connection.ts`` ``closeDatabase`` lines 98–112).

### Apple system Python guard

Per ADR-004 §Consequences, the loadable-extensions probe is the first
operation that touches ``conn.enable_load_extension``. We delegate the guard
to :func:`lossless_hermes.engine._check_sqlite_extension_loading` so the
``__init__``-time hook in :mod:`lossless_hermes.engine` and the
DB-open-time hook here share one error message
(:data:`lossless_hermes.engine.APPLE_SYSTEM_PYTHON_MSG`). The guard fires
**before** any other PRAGMA — operators see one actionable
:class:`RuntimeError` ("install Homebrew / pyenv / uv / python.org Python"),
not an obscure ``AttributeError`` deep in the load path.

### WAL-on-network-filesystem fallback (storage.md §10.4)

WAL mode requires shared-memory + fcntl byte-range locks that don't work on
NFS/SMB/some FUSE mounts. ``hermes_state.py`` solves this by catching the
``locking protocol`` family of errors and falling back to ``journal_mode =
DELETE`` (the pre-WAL default). storage.md §10.4 mandates we mirror the
behavior — but importing from ``hermes_state.py`` would create a hard
hermes-agent dependency for the storage layer (forbidden by ADR-007), and
the upstream-refactor-to-``db_utils.py`` has not landed
(``epics/01-storage/01-01-db-connection.md`` §4 "if upstream refactor stalls,
inline the function locally"). So we inline the markers + helper here.

### Why a registry

The TS code's ``connectionsByPath`` + ``connectionIndex`` exists to support
test fixtures that close-by-path. Python ``sqlite3.Connection`` is not
thread-shareable by default (``check_same_thread=True`` blocks cross-thread
use), so the registry is keyed by ``(path, thread_id)`` per ADR-007 — one
connection per thread per path. Tests call
``close_lcm_connection(path=X)`` to close all threads' connections for that
path; that's the only public registry consumer at v0.

### Function inventory

| Function | Purpose |
|---|---|
| :func:`open_lcm_db` | Main factory; opens + configures + registers a Connection. |
| :func:`close_lcm_db` | Run ``PRAGMA optimize`` (best-effort) then close + unregister. |
| :func:`close_lcm_connection` | Close all tracked connections for a path (test fixtures). |
| :func:`is_in_memory_path` | Path-helper: ``":memory:"`` or ``file::memory:...``. |
| :func:`get_file_backed_database_path` | Path-helper: absolute path for non-memory inputs, else ``None``. |
| :func:`normalize_path` | Path-helper: canonical registry key. |
| :func:`assert_foreign_keys_enabled` | Reads back ``PRAGMA foreign_keys`` to confirm enforcement. |

See:

* ``docs/adr/004-sqlite3-backend.md`` — stdlib primary, apsw fallback.
* ``docs/adr/007-hermes-as-dependency.md`` — no hard import on Hermes.
* ``docs/adr/017-sync-vs-async-db.md`` — synchronous-by-design.
* ``docs/spike-results/001-sqlite-vec-python.md`` — load pattern + Apple guard.
* ``docs/porting-guides/storage.md`` §3 — PRAGMA + connection setup spec.
* ``docs/porting-guides/storage.md`` §10.4 — WAL-on-NFS fallback.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, Set, Tuple, Union, runtime_checkable

import sqlite_vec

from lossless_hermes.engine import (
    APPLE_SYSTEM_PYTHON_MSG,
    _check_sqlite_extension_loading,
    _has_sqlite_extension_loading,
)

if TYPE_CHECKING:
    pass

# Optional ``apsw`` dependency probe (ADR-004 §"apsw fallback is opt-in"; the
# ``[apsw]`` extra is documented in pyproject.toml). We probe via
# ``importlib.util.find_spec`` rather than ``import apsw`` so static type
# checkers (``ty``) don't try to resolve the apsw module when the [apsw]
# extra isn't installed — CI runs ``uv sync --locked --extra dev`` which
# intentionally omits [apsw], and a top-level ``import apsw`` triggers
# ``error[unresolved-import]`` even inside a try/except (resolution is
# decoupled from runtime import in static analysis). The actual ``import
# apsw`` happens lazily inside :func:`_open_with_apsw` where it's guarded
# by ``HAS_APSW`` and the call site is annotated for ty.
import importlib.util

HAS_APSW: bool = importlib.util.find_spec("apsw") is not None

__all__ = [
    "HAS_APSW",
    "SQLITE_BUSY_TIMEOUT_GATEWAY_MS",
    "SQLITE_BUSY_TIMEOUT_MS",
    "SQLITE_BUSY_TIMEOUT_WORKER_MS",
    "Connection",
    "DbRole",
    "assert_foreign_keys_enabled",
    "close_lcm_connection",
    "close_lcm_db",
    "get_file_backed_database_path",
    "is_in_memory_path",
    "normalize_path",
    "open_db",
    "open_lcm_db",
    "try_load_sqlite_vec",
    "vec0_version",
]

_log = logging.getLogger("lossless_hermes.db.connection")


# ---------------------------------------------------------------------------
# Connection Protocol — uniform stdlib/apsw API surface (05-04 AC item 3)
# ---------------------------------------------------------------------------


@runtime_checkable
class Connection(Protocol):
    """The four-method surface that lossless-hermes calls on a DB connection.

    Both stdlib :class:`sqlite3.Connection` and :class:`apsw.Connection`
    satisfy this Protocol structurally. The :func:`open_db` factory returns
    one of these — callers should annotate with :class:`Connection` rather
    than the concrete driver type to keep the apsw-vs-stdlib swap cheap
    (ADR-004 §Open questions item 5 + the 05-04 spec).

    Only the four methods listed here are part of the uniform contract. If a
    caller needs a driver-specific feature (e.g. ``sqlite3.Connection``'s
    ``set_trace_callback``), it should either request that via the Protocol
    via a follow-up issue or downcast with an explicit ``isinstance`` check.

    The Protocol is marked :func:`typing.runtime_checkable` so unit tests can
    assert ``isinstance(conn, Connection)`` to confirm the returned object
    structurally matches the contract.
    """

    def execute(self, sql: str, parameters: Any = ..., /) -> Any:
        """Execute a single SQL statement. Returns a driver-specific cursor."""
        ...

    def executemany(self, sql: str, parameters: Any, /) -> Any:
        """Execute the same SQL against many parameter sets."""
        ...

    def commit(self) -> None:
        """Commit the current transaction.

        ``apsw`` runs in autocommit mode by default, so ``commit()`` is a
        no-op there — implementations differ but the surface is the same.
        """
        ...

    def close(self) -> None:
        """Close the underlying connection. Idempotent in both drivers."""
        ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Matches ``connection.ts`` line 12 (``SQLITE_BUSY_TIMEOUT_MS = 30_000``).
# 30 s accommodates high-concurrency multi-agent setups where ≥10 writers
# contend on the WAL. The 5 s default proved insufficient in production —
# see ``storage.md`` §3 table and the comment in ``connection.ts:8-11``.
SQLITE_BUSY_TIMEOUT_MS = 30_000

# Role-based busy_timeout split for :func:`open_db` per the
# ``epics/05-embeddings/05-04-vec0-load-pattern.md`` acceptance criteria.
# Rationale (see ADR-018 §"Concurrency model" + the 05-04 spec):
#
# * Gateway connections (foreground tool calls + hooks) get the full
#   ``SQLITE_BUSY_TIMEOUT_GATEWAY_MS = 30_000`` — they're driving user-facing
#   latency and must wait out contention rather than fail.
# * Worker connections (embedding backfill / entity extraction /
#   condensation maintenance) get ``SQLITE_BUSY_TIMEOUT_WORKER_MS = 5_000``
#   so they yield to the gateway under contention. Workers can retry on the
#   next tick; a foreground call cannot.
#
# Gateway-always-wins is the design contract.
SQLITE_BUSY_TIMEOUT_GATEWAY_MS = 30_000
SQLITE_BUSY_TIMEOUT_WORKER_MS = 5_000

# Type alias for :func:`open_db`'s ``role`` keyword. Constrained at the
# type-checker level so callers get a clear error on typos like
# ``role="forground"`` rather than a silent fall-through to the worker
# busy_timeout.
DbRole = Literal["gateway", "worker"]

# WAL incompatibility markers — substrings appearing in
# ``sqlite3.OperationalError`` messages when the filesystem can't host WAL.
# Mirrors ``hermes_state.py:54-58`` ``_WAL_INCOMPAT_MARKERS`` verbatim so
# both DBs (state.db + lcm.db) classify NFS/SMB/FUSE errors identically.
_WAL_INCOMPAT_MARKERS: Tuple[str, ...] = (
    "locking protocol",  # SQLITE_PROTOCOL on NFS/SMB
    "not authorized",  # Some FUSE mounts block WAL pragma outright
    "disk i/o error",  # Flaky network FS during WAL setup
)

# ---------------------------------------------------------------------------
# Module-level state (registry + lock + WAL-fallback dedup)
# ---------------------------------------------------------------------------

# Per ADR-007 the registry is keyed by ``(normalized_path, thread_id)``.
# Stdlib ``sqlite3.Connection`` is not thread-shareable by default, so each
# thread keeps its own Connection per path. Tests call
# ``close_lcm_connection(path=X)`` to close all threads' connections for X.
_ConnectionKey = Tuple[str, int]
_connections_by_path: dict[str, Set[sqlite3.Connection]] = {}
_connection_index: dict[int, str] = {}  # id(conn) -> normalized path
_registry_lock = threading.Lock()

# WAL-fallback warning dedup. Without this, repeat opens on an NFS-mounted
# ``lcm.db`` would spam the log with one identical warning per connection.
_wal_fallback_warned_paths: Set[str] = set()
_wal_fallback_warned_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Path helpers (ports ``connection.ts`` lines 17-41)
# ---------------------------------------------------------------------------


def _normalize_db_path_input(db_path: Union[str, Path]) -> str:
    """Coerce + trim ``db_path`` to a string.

    Matches ``connection.ts:normalizeDbPathInput`` lines 17-19: if the input
    is a string, trim it; otherwise return ``""``. ``Path`` instances are
    converted to their string form first.
    """
    if isinstance(db_path, Path):
        return str(db_path).strip()
    if isinstance(db_path, str):
        return db_path.strip()
    return ""


def is_in_memory_path(db_path: Union[str, Path]) -> bool:
    """Return ``True`` when ``db_path`` denotes an in-memory database.

    Ports ``connection.ts:isInMemoryPath`` lines 21-24. Accepts the literal
    ``":memory:"`` and any URI starting with ``file::memory:`` (the latter
    is SQLite's URI form, e.g. ``file::memory:?cache=shared``).
    """
    normalized = _normalize_db_path_input(db_path)
    return normalized == ":memory:" or normalized.startswith("file::memory:")


def get_file_backed_database_path(db_path: Union[str, Path]) -> str | None:
    """Return the absolute path for a file-backed DB, or ``None``.

    Ports ``connection.ts:getFileBackedDatabasePath`` lines 26-32. Empty
    inputs and in-memory paths return ``None``; everything else is resolved
    to an absolute path via :func:`os.path.abspath` (matches Node's
    ``path.resolve`` semantics for the common case of a project-relative
    input).
    """
    trimmed = _normalize_db_path_input(db_path)
    if not trimmed or is_in_memory_path(trimmed):
        return None
    return os.path.abspath(trimmed)


def normalize_path(db_path: Union[str, Path]) -> str:
    """Return the canonical registry key for ``db_path``.

    Ports ``connection.ts:normalizePath`` lines 34-41. For file-backed
    paths this is the absolute path; for in-memory or empty inputs this
    is the literal ``":memory:"`` (so anonymous in-memory DBs share a
    bucket, matching the TS behavior).
    """
    file_backed = get_file_backed_database_path(db_path)
    if file_backed is not None:
        return file_backed
    trimmed = _normalize_db_path_input(db_path)
    return trimmed if trimmed else ":memory:"


def _ensure_db_directory(db_path: Union[str, Path]) -> None:
    """Create the parent directory for a file-backed DB (``mkdir -p``).

    Ports ``connection.ts:ensureDbDirectory`` lines 43-49. In-memory paths
    are a no-op.
    """
    file_backed = get_file_backed_database_path(db_path)
    if file_backed is None:
        return
    parent = os.path.dirname(file_backed)
    if parent:
        os.makedirs(parent, exist_ok=True)


# ---------------------------------------------------------------------------
# WAL fallback helper (mirrors hermes_state.py:128 ``apply_wal_with_fallback``)
# ---------------------------------------------------------------------------


def _apply_wal_with_fallback(conn: sqlite3.Connection, *, db_label: str) -> str:
    """Set ``journal_mode = WAL``; fall back to DELETE on NFS/SMB/FUSE.

    Mirrors ``hermes-agent/hermes_state.py:128`` ``apply_wal_with_fallback``
    function lines 128-161. Inlined here per ``storage.md`` §10.4 (the
    upstream refactor to ``db_utils.py`` has not landed; importing from
    ``hermes_state.py`` would couple lossless-hermes to the host's
    internals, forbidden by ADR-007).

    Returns the journal mode actually set (``"wal"`` or ``"delete"``).
    """
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        return "wal"
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(marker in msg for marker in _WAL_INCOMPAT_MARKERS):
            # Unrelated OperationalError — don't silently swallow it.
            raise
        _log_wal_fallback_once(db_label, exc)
        conn.execute("PRAGMA journal_mode = DELETE")
        return "delete"


def _log_wal_fallback_once(db_label: str, exc: Exception) -> None:
    """Emit one WARNING per ``db_label`` about the WAL→DELETE fallback.

    Mirrors ``hermes_state.py:164`` ``_log_wal_fallback_once`` so repeat
    opens of the same ``lcm.db`` on an NFS mount don't fill the log.
    """
    with _wal_fallback_warned_lock:
        if db_label in _wal_fallback_warned_paths:
            return
        _wal_fallback_warned_paths.add(db_label)
    _log.warning(
        "%s: WAL journal_mode unsupported on this filesystem (%s) - "
        "falling back to journal_mode=DELETE (slower rollback-journal "
        "mode; reduces concurrency but works on NFS/SMB/FUSE). See "
        "https://www.sqlite.org/wal.html for details. This warning "
        "fires once per process per database.",
        db_label,
        exc,
    )


# ---------------------------------------------------------------------------
# Foreign-key invariant readback (mirrors concurrency/model.ts:116)
# ---------------------------------------------------------------------------


def assert_foreign_keys_enabled(conn: sqlite3.Connection) -> None:
    """Confirm ``PRAGMA foreign_keys`` is ``ON`` for this connection.

    Reads the PRAGMA back via ``PRAGMA foreign_keys`` and asserts the
    returned row is ``1``. If it isn't, every ``ON DELETE CASCADE`` in
    the schema silently no-ops — a class of data-integrity bug that's
    invisible at SQL level.

    Ports ``concurrency/model.ts:116`` ``assertForeignKeysEnabled`` lines
    116-126 verbatim. The TS comment "v4.1 B.fix - Gap 7: verify the
    PRAGMA actually took effect. Catches future regressions where a code
    path opens a connection that bypasses configureConnection and leaves
    FK enforcement off" applies here too — call this after the
    ``foreign_keys = ON`` PRAGMA in :func:`open_lcm_db`, and from any hot
    path that wants to defend against a non-sanctioned connection slip.

    Raises:
        RuntimeError: ``foreign_keys`` is not ``1``.
    """
    # LCM v4.1 B.fix (Gap 7): verify the PRAGMA actually took effect.
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    if not row or row[0] != 1:
        raise RuntimeError(
            "[lossless_hermes.db.connection] foreign_keys is not ON for this "
            "connection - every ON DELETE CASCADE in the schema would silently "
            "no-op. Ensure the connection passes through open_lcm_db() in "
            "lossless_hermes/db/connection.py, or set PRAGMA foreign_keys = ON "
            "explicitly."
        )


# ---------------------------------------------------------------------------
# Connection registry (ports ``connection.ts`` lines 72-96)
# ---------------------------------------------------------------------------


def _track_connection(db_path: Union[str, Path], conn: sqlite3.Connection) -> None:
    """Add ``conn`` to the per-path connection set under the registry lock."""
    key = normalize_path(db_path)
    with _registry_lock:
        entries = _connections_by_path.get(key)
        if entries is None:
            entries = set()
            _connections_by_path[key] = entries
        entries.add(conn)
        _connection_index[id(conn)] = key


def _untrack_connection(conn: sqlite3.Connection) -> None:
    """Remove ``conn`` from the registry; no-op if absent."""
    with _registry_lock:
        key = _connection_index.pop(id(conn), None)
        if key is None:
            return
        entries = _connections_by_path.get(key)
        if entries is not None:
            entries.discard(conn)
            if not entries:
                _connections_by_path.pop(key, None)


# ---------------------------------------------------------------------------
# Configuration (ports ``connection.ts:configureConnection`` lines 51-70)
# ---------------------------------------------------------------------------


def _configure_connection(conn: sqlite3.Connection, *, db_label: str) -> sqlite3.Connection:
    """Apply the seven PRAGMAs in the order documented in storage.md §3.

    Order is load-bearing — ``foreign_keys`` must precede the readback
    assertion, and ``journal_mode = WAL`` must run first because the
    fallback path's ``journal_mode = DELETE`` is the recovery branch.

    Ports ``connection.ts:configureConnection`` lines 51-70 with one
    deviation: the WAL pragma runs through :func:`_apply_wal_with_fallback`
    so NFS/SMB/FUSE-hosted DBs degrade gracefully (storage.md §10.4).
    """
    # 1. journal_mode = WAL (with NFS/SMB fallback). storage.md §3 row 1.
    _apply_wal_with_fallback(conn, db_label=db_label)

    # 2. busy_timeout = 30000ms. storage.md §3 row 2 + connection.ts:53.
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")

    # 3. foreign_keys = ON. storage.md §3 row 3 + connection.ts:54.
    conn.execute("PRAGMA foreign_keys = ON")

    # 4. Assert foreign_keys actually took effect. storage.md §3 row 4 +
    # connection.ts:55-59 (v4.1 B.fix Gap 7).
    assert_foreign_keys_enabled(conn)

    # 5. cache_size = -65536 (64 MB). storage.md §3 row 5 + connection.ts:62.
    conn.execute("PRAGMA cache_size = -65536")

    # 6. synchronous = NORMAL. storage.md §3 row 6 + connection.ts:66.
    conn.execute("PRAGMA synchronous = NORMAL")

    # 7. temp_store = MEMORY. storage.md §3 row 7 + connection.ts:68.
    conn.execute("PRAGMA temp_store = MEMORY")

    return conn


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Enable extensions, load sqlite-vec, disable extensions.

    Ports spike-001 §"Load pattern" verbatim. The
    ``enable_load_extension(False)`` step is non-negotiable per ADR-004
    §Consequences ("Tightens the attack surface and matches the spike-001
    recommended pattern").

    The Apple system Python guard (:func:`_check_sqlite_extension_loading`)
    fires **before** we touch ``enable_load_extension`` so the failure
    surface is one clear :class:`RuntimeError` referencing
    ``APPLE_SYSTEM_PYTHON_MSG``, not an obscure :class:`AttributeError`.
    """
    _check_sqlite_extension_loading()
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        # Disable extensions even if ``sqlite_vec.load`` fails — leaving the
        # flag on is a latent injection-risk window.
        conn.enable_load_extension(False)


# ---------------------------------------------------------------------------
# Public API: open / close
# ---------------------------------------------------------------------------


def open_lcm_db(
    path: Union[str, Path],
    *,
    driver: Literal["sqlite3", "apsw"] = "sqlite3",
) -> sqlite3.Connection:
    """Open a fully-configured LCM database connection.

    The single sanctioned factory per ADR-004 §Consequences invariant —
    "Every SQLite import path lives behind ``open_lcm_db()``." Performs,
    in order:

    1. Parent-directory mkdir for file-backed paths.
    2. ``sqlite3.connect(path, check_same_thread=False)`` for the stdlib
       driver; ``apsw.Connection(path)`` for the apsw driver if the extra
       is installed (per ADR-004 the apsw extra is opt-in).
    3. Apple system Python guard via
       :func:`lossless_hermes.engine._check_sqlite_extension_loading`.
    4. ``enable_load_extension(True)`` → ``sqlite_vec.load(conn)`` →
       ``enable_load_extension(False)`` (spike-001 §"Load pattern").
    5. PRAGMA tunings via :func:`_configure_connection` (storage.md §3
       order).
    6. Registry track for test-fixture close-by-path.

    Args:
        path: Filesystem path to ``lcm.db`` (or ``":memory:"`` /
            ``"file::memory:..."`` for in-memory DBs). Accepts both
            :class:`str` and :class:`pathlib.Path`.
        driver: Which underlying driver to use. ``"sqlite3"`` (default)
            uses the Python stdlib. ``"apsw"`` requires the
            ``lossless-hermes[apsw]`` extra; raises :class:`ImportError`
            with an actionable message if apsw is not installed.

    Returns:
        A configured :class:`sqlite3.Connection` (or apsw connection when
        ``driver="apsw"``) with extensions disabled, sqlite-vec loaded,
        and all 7 PRAGMAs applied.

    Raises:
        RuntimeError: Apple system Python lacks ``enable_load_extension``
            (the guard fires with a documented install hint).
        RuntimeError: ``foreign_keys = ON`` did not take effect.
        ImportError: ``driver="apsw"`` but the apsw extra is not installed.
        sqlite3.OperationalError: Any non-WAL-related PRAGMA or SQL error.
    """
    if driver not in ("sqlite3", "apsw"):
        raise ValueError(f"open_lcm_db: driver must be 'sqlite3' or 'apsw', got {driver!r}")
    if driver == "apsw":
        # Defer the apsw path to the issue that adds the [apsw] CI lane.
        # The contract is documented but unimplemented at v0 to keep this
        # PR focused on the stdlib path (per the issue spec — apsw extra
        # exists in pyproject.toml as a pin but no code path consumes it
        # until the apsw lane lands).
        raise NotImplementedError(
            "open_lcm_db(driver='apsw'): apsw driver landing in a follow-up "
            "issue (see ADR-004 §Open questions item 5). For v0, install "
            "lossless-hermes with the default stdlib sqlite3 driver."
        )

    _ensure_db_directory(path)

    # Normalize once and reuse for the registry label + log label so a
    # single ``lcm.db`` path produces one warning per process even if
    # multiple threads open it.
    canonical_path = normalize_path(path)
    db_label = canonical_path

    # The TS ``new DatabaseSync(dbPath, { allowExtension: true })`` maps to
    # ``sqlite3.connect(path)`` + ``enable_load_extension(True)`` later in
    # ``_load_sqlite_vec``. ``check_same_thread=False`` is required because
    # the registry key carries ``thread_id`` per ADR-007 — connections never
    # cross threads in practice, but the stdlib check is too strict for
    # the registry walk in :func:`close_lcm_connection` (test fixtures may
    # close from a different thread than the opener).
    #
    # Path arg accepts both ``str`` and ``Path`` via ``str(...)``.
    path_str = str(path) if isinstance(path, Path) else path
    conn = sqlite3.connect(path_str, check_same_thread=False)

    try:
        _load_sqlite_vec(conn)
        _configure_connection(conn, db_label=db_label)
    except Exception:
        # If any setup step fails, close the half-configured connection
        # before re-raising so callers don't leak handles.
        try:
            conn.close()
        except Exception:  # noqa: BLE001 -- cleanup must not mask the real exc
            pass
        raise

    _track_connection(path, conn)
    return conn


def close_lcm_db(conn: sqlite3.Connection | None) -> None:
    """Run ``PRAGMA optimize`` (best-effort) then close + untrack.

    Ports ``connection.ts:closeDatabase`` lines 98-112. The
    ``PRAGMA optimize`` step refreshes query-planner stats for tables that
    changed since the last optimize; we swallow ``OperationalError``
    because a busy/read-only DB shouldn't block close.

    Calling with ``None`` is a no-op (matches the TS ``if (!db) return``
    on line 99).
    """
    if conn is None:
        return
    try:
        try:
            conn.execute("PRAGMA optimize")
        except (sqlite3.OperationalError, sqlite3.ProgrammingError):
            # Best-effort — a SQLITE_BUSY / SQLITE_READONLY (OperationalError)
            # or an already-closed conn (ProgrammingError, possible during
            # cleanup-after-failure paths) must not skip the close call below.
            # connection.ts:105 catches every error category for the same
            # reason.
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001 -- callers are shutting down
            pass
    finally:
        _untrack_connection(conn)


def close_lcm_connection(
    target: Union[str, Path, sqlite3.Connection, None] = None,
) -> None:
    """Close tracked LCM connections.

    Ports ``connection.ts:closeLcmConnection`` lines 144-168. Three modes:

    * ``target=None`` — close **all** tracked connections (cleanup at
      process shutdown or end-of-test-session).
    * ``target=path`` (``str`` or :class:`Path`) — close all tracked
      connections for that normalized path. Used by test fixtures that
      need to ensure no leftover handles hold the WAL.
    * ``target=conn`` — close exactly that one connection (rare; prefer
      :func:`close_lcm_db`).

    Args:
        target: Connection, path, or ``None`` (close-all).
    """
    if isinstance(target, sqlite3.Connection):
        close_lcm_db(target)
        return

    if isinstance(target, (str, Path)):
        key = normalize_path(target)
        with _registry_lock:
            entries = _connections_by_path.get(key)
            conns_to_close = list(entries) if entries else []
        for conn in conns_to_close:
            close_lcm_db(conn)
        # The per-path bucket is removed when the last connection is
        # untracked in ``close_lcm_db`` (via ``_untrack_connection``).
        return

    # target is None: walk every tracked connection.
    with _registry_lock:
        all_conns = [conn for entries in _connections_by_path.values() for conn in list(entries)]
    for conn in all_conns:
        close_lcm_db(conn)
    with _registry_lock:
        _connections_by_path.clear()
        _connection_index.clear()


# ---------------------------------------------------------------------------
# 05-04: Public sqlite-vec helpers (ports store.ts:122-159)
# ---------------------------------------------------------------------------
#
# The TS ``candidateVec0Paths`` function (store.ts:83-100) is DROPPED in the
# Python port — see spike 001 §"Recommended Python stack". The PyPI
# ``sqlite_vec`` package auto-discovers its bundled extension via
# ``sqlite_vec.load(conn)``, removing the need for the env-var / cwd /
# extensions-dir / homedir search the TS code performs. Future contributors:
# do not reintroduce that search; if a custom path is ever needed, the
# escape hatch is :meth:`sqlite_vec.loadable_path` (returns the bundled
# extension's filesystem path which can then be passed to a driver-specific
# load API).


def try_load_sqlite_vec(conn: Connection, *, silent: bool = False) -> bool:
    """Best-effort load of the ``sqlite-vec`` extension on ``conn``.

    Ports ``lossless-claw/src/embeddings/store.ts:122-146`` ``tryLoadSqliteVec``.
    Returns :data:`True` on success, :data:`False` on any failure. Callers
    that want a graceful degrade (e.g. ``runSemanticSearch`` raising
    ``SemanticSearchUnavailableError`` when this returns :data:`False`)
    consume this; callers that demand vec0 should use :func:`open_db` which
    raises if the load fails.

    Idempotent — :func:`sqlite_vec.load` registers vec0 once per process,
    subsequent calls on already-loaded connections are no-ops.

    Args:
        conn: An open SQLite connection (stdlib :class:`sqlite3.Connection`
            or apsw equivalent). Must expose ``enable_load_extension`` on
            the stdlib path; the apsw path uses ``loadextension`` which
            ``sqlite_vec.load`` handles internally.
        silent: When :data:`True`, suppress the WARNING log on failure.
            Default :data:`False`. Matches the TS ``opts.silent`` flag.

    Returns:
        :data:`True` if vec0 SQL is available after the call, else
        :data:`False`.
    """
    try:
        # Stdlib ``sqlite3.Connection`` exposes ``enable_load_extension``;
        # apsw exposes ``enableloadextension`` (no underscore). The
        # ``sqlite_vec.load`` helper handles both internally, but we still
        # want to disable extensions afterwards to tighten the attack
        # surface (spike-001 recommendation).
        enable = getattr(conn, "enable_load_extension", None)
        if enable is not None:
            enable(True)
        sqlite_vec.load(conn)  # type: ignore[arg-type]
        if enable is not None:
            enable(False)
        return True
    except (AttributeError, sqlite3.OperationalError) as exc:
        if not silent:
            _log.warning("[db.connection] failed to load sqlite-vec: %s", exc)
        return False


def vec0_version(conn: Connection) -> str | None:
    """Return the loaded ``sqlite-vec`` version string, or :data:`None`.

    Ports ``lossless-claw/src/embeddings/store.ts:152-159`` ``vec0Version``.
    Cheap probe — runs ``SELECT vec_version()`` against ``conn``. Returns
    :data:`None` when the extension is not loaded (the SQL raises
    :class:`sqlite3.OperationalError` in stdlib; apsw raises its own
    ``ExecutionError``). The :class:`Exception` catch absorbs both — we
    deliberately do not narrow further because the contract is "any failure
    means not-loaded" and a driver-specific ``ExecutionError`` should not
    leak through.

    Used by ``/lcm health`` (Epic 08) and the ``runSemanticSearch``
    precondition check (#05-08) to decide whether to attempt KNN.
    """
    try:
        row = conn.execute("SELECT vec_version()").fetchone()
    except Exception:  # noqa: BLE001 -- spec: any failure ⇒ not-loaded
        return None
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# 05-04: open_db() — role-aware factory (issue #05-04)
# ---------------------------------------------------------------------------


class _ApswConnectionAdapter:
    """Adapt :class:`apsw.Connection` to the :class:`Connection` Protocol.

    apsw provides ``execute``, ``executemany``, and ``close`` natively but
    does **not** expose ``commit`` — it runs in autocommit mode by default,
    so writes are committed immediately unless the caller explicitly
    issues ``BEGIN``. To satisfy the four-method contract documented in
    the 05-04 spec ("``.execute``, ``.executemany``, ``.commit``,
    ``.close``"), we wrap apsw connections in this adapter; ``commit()``
    issues a SQL ``COMMIT`` if a transaction is open and is a no-op
    otherwise. This mirrors stdlib :meth:`sqlite3.Connection.commit`
    behavior closely enough that caller code written against the
    Protocol is portable.

    The adapter forwards every other attribute (and the underlying
    ``execute`` / ``executemany`` / ``close``) to the wrapped apsw conn
    via :meth:`__getattr__`, so callers can still reach apsw-specific
    APIs (``loadextension``, ``set_busy_timeout``, etc.) when they
    explicitly downcast — the Protocol surface is the uniform path; the
    underlying conn is the escape hatch.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str, parameters: Any = (), /) -> Any:
        # apsw's ``execute`` accepts ``(sql,)`` or ``(sql, bindings)``;
        # mimic stdlib's PEP-249 signature where ``parameters`` defaults
        # to ``()``.
        if parameters == ():
            return self._conn.execute(sql)
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> Any:
        return self._conn.executemany(sql, parameters)

    def commit(self) -> None:
        # apsw is in autocommit mode by default. If a caller wrapped
        # multi-statement work in ``BEGIN``/``COMMIT`` explicitly, the
        # ``COMMIT`` was already issued via :meth:`execute`. We invoke
        # the SQL form here for cases where the caller treats the conn
        # as a stdlib :class:`sqlite3.Connection` and never typed
        # ``COMMIT`` themselves — ``getautocommit()`` returns ``False``
        # only when a transaction is open, so the conditional avoids
        # the "no transaction is active" SQL error on idle conns.
        if not self._conn.getautocommit():
            self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def __getattr__(self, name: str) -> Any:
        # Forward anything not on the adapter to the underlying apsw
        # connection — escape hatch for apsw-specific APIs.
        return getattr(self._conn, name)


def _open_with_apsw(path: Union[str, Path], role: DbRole) -> Connection:
    """Open ``path`` via the optional ``apsw`` driver. Isolated for
    contract-stability per the 05-04 spec.

    The apsw driver differs structurally from stdlib :mod:`sqlite3`:

    * Extension load method is ``enableloadextension`` (no underscore).
    * No PEP-249 :meth:`cursor` boilerplate — :meth:`execute` lives on the
      connection itself, just like our :class:`Connection` Protocol.
    * Autocommit by default — explicit :meth:`commit` is a no-op outside a
      ``BEGIN`` block. We still call :meth:`commit` from our public API for
      uniform-surface guarantees; apsw silently absorbs the call.

    All apsw-specific code lives behind this single helper so the surface
    swap stays cheap if ADR-004 ever flips the default driver. Per the
    05-04 spec the apsw fallback is opt-in (the ``[apsw]`` extra) AND only
    fires when the stdlib path fails with :class:`sqlite3.OperationalError`
    — it is not a primary code path.

    Args:
        path: Filesystem path to ``lcm.db``. In-memory paths are accepted
            but typically not useful for the apsw fallback (the fallback
            exists for filesystem-level stdlib failures).
        role: ``"gateway"`` or ``"worker"`` — determines the busy_timeout.

    Returns:
        An apsw connection structurally compatible with the :class:`Connection`
        Protocol.

    Raises:
        ImportError: ``[apsw]`` extra is not installed.
        Exception: Any apsw-side failure propagates to the caller; the
            stdlib path's :class:`OperationalError` already triggered the
            fallback, so an apsw-side failure is a real configuration error.
    """
    if not HAS_APSW:
        raise ImportError(
            "lossless-hermes: apsw fallback requested but the [apsw] extra "
            "is not installed. Re-install with "
            "`pip install lossless-hermes[apsw]` to enable the apsw driver. "
            "(See ADR-004 §'apsw fallback is opt-in'.)"
        )

    # Lazy import — ``apsw`` is an optional extra and may not be on the
    # import path in CI (the [apsw] extra is opt-in per ADR-005). We use
    # :func:`importlib.import_module` rather than ``import apsw`` so static
    # type checkers (``ty``) don't try to resolve the module when [apsw]
    # isn't installed; the dynamic call yields ``Any``, which is the
    # right shape here because the caller-facing API is the
    # :class:`_ApswConnectionAdapter` wrapper (the apsw-specific type
    # never leaks past this helper). ``HAS_APSW`` was probed via
    # :func:`importlib.util.find_spec` at module load (see top of file)
    # so this branch is only reached when apsw IS importable.
    _apsw = importlib.import_module("apsw")

    path_str = str(path) if isinstance(path, Path) else path

    # apsw's :class:`Connection` accepts a path string (or ``":memory:"``).
    # No ``check_same_thread`` kwarg — apsw is more permissive about
    # cross-thread access than stdlib, though we still recommend
    # per-thread connections (spike 001 §Gotchas).
    conn = _apsw.Connection(path_str)

    try:
        # apsw uses ``enableloadextension`` (no underscore) — keep the
        # apsw-specific name confined to this helper. ``sqlite_vec.load``
        # then dispatches the right load API for whichever driver is
        # passed.
        conn.enableloadextension(True)
        # sqlite_vec.load's static signature names sqlite3.Connection but
        # the runtime behavior is duck-typed and works on apsw connections
        # (spike-001 verified). Suppress the ty diagnostic here — the
        # ``_ApswConnectionAdapter`` wraps the result for the public API
        # so callers never see the apsw-specific type leak through.
        sqlite_vec.load(conn)  # type: ignore[arg-type]
        conn.enableloadextension(False)

        # Mirror the stdlib path's PRAGMA set: journal_mode WAL + role-based
        # busy_timeout + foreign_keys + cache_size + synchronous +
        # temp_store. apsw uses ``conn.execute`` (same as stdlib), so the
        # SQL statements are identical. We skip the WAL-fallback helper
        # here — apsw's :class:`OperationalError` is a different class
        # hierarchy and the fallback's marker-string heuristics don't
        # transfer cleanly; if WAL fails on apsw the caller sees the raw
        # error (acceptable: apsw is the opt-in fallback, not the default).
        conn.execute("PRAGMA journal_mode = WAL")
        busy_timeout = _busy_timeout_for_role(role)
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA cache_size = -65536")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
    except Exception:
        # If any setup step fails, close the half-configured connection
        # before re-raising so callers don't leak handles.
        try:
            conn.close()
        except Exception:  # noqa: BLE001 -- cleanup must not mask the real exc
            pass
        raise

    # Wrap in the adapter so the returned object structurally satisfies
    # the :class:`Connection` Protocol (apsw lacks ``commit``).
    return _ApswConnectionAdapter(conn)


def _busy_timeout_for_role(role: DbRole) -> int:
    """Return the ``busy_timeout`` ms value for ``role``.

    Internal helper consumed by both :func:`open_db` and
    :func:`_open_with_apsw` so the role-based split is documented in one
    place.

    Raises:
        ValueError: ``role`` is not one of ``"gateway"`` / ``"worker"``.
            Defensive guard for callers that bypass the type-checker (the
            :data:`DbRole` :class:`typing.Literal` catches typos at static
            check time, but a stray :data:`str` at runtime would otherwise
            silently mis-configure the timeout).
    """
    if role == "gateway":
        return SQLITE_BUSY_TIMEOUT_GATEWAY_MS
    if role == "worker":
        return SQLITE_BUSY_TIMEOUT_WORKER_MS
    raise ValueError(
        f"open_db: role must be 'gateway' or 'worker', got {role!r}. "
        "See ADR-018 §'Concurrency model' for the gateway/worker split."
    )


def open_db(
    path: Union[str, Path],
    *,
    role: DbRole = "gateway",
) -> Connection:
    """Role-aware connection factory used by the embeddings subsystem.

    Companion to :func:`open_lcm_db`. The two factories differ in two
    respects:

    1. ``role`` selects the ``busy_timeout`` per ADR-018 + the 05-04 spec
       — gateway connections get ``30_000`` ms; worker connections get
       ``5_000`` ms. Gateway-always-wins contention is the design.
    2. ``open_db`` runtime-falls-through to the apsw driver if the stdlib
       path raises :class:`sqlite3.OperationalError` AND the ``[apsw]``
       extra is installed. This is rare on supported platforms (Homebrew /
       pyenv / uv Python on macOS + manylinux Python on Linux all expose
       loadable extensions) — the fallback exists to cover the
       documented edge cases in ADR-004 (e.g. a custom-built Python with
       ``--disable-loadable-sqlite-extensions``).

    Apple system Python guard fires **before** the apsw fallback (the
    ``RuntimeError`` from :func:`_check_sqlite_extension_loading` is not
    an :class:`OperationalError`), so a `/usr/bin/python3` user gets the
    actionable install hint, not a silent apsw fall-through.

    Args:
        path: Filesystem path to ``lcm.db`` (or ``":memory:"`` /
            ``"file::memory:..."`` for in-memory DBs). Both :class:`str`
            and :class:`pathlib.Path` are accepted.
        role: ``"gateway"`` (default — 30 s busy_timeout) or ``"worker"``
            (5 s busy_timeout). See module-level constants
            :data:`SQLITE_BUSY_TIMEOUT_GATEWAY_MS` /
            :data:`SQLITE_BUSY_TIMEOUT_WORKER_MS`.

    Returns:
        A :class:`Connection`-compatible object (stdlib
        :class:`sqlite3.Connection` on the primary path; an
        :class:`apsw.Connection` on the fallback path).

    Raises:
        RuntimeError: Apple system Python lacks ``enable_load_extension``
            (the guard fires with the documented install hint).
        ValueError: ``role`` is not ``"gateway"`` / ``"worker"``.
        sqlite3.OperationalError: Stdlib path failed AND ``[apsw]`` extra
            not installed (no fallback available).
        Exception: apsw fallback was attempted but also failed.
    """
    # Validate role up-front so the ValueError fires before any FS work.
    busy_timeout = _busy_timeout_for_role(role)

    _ensure_db_directory(path)

    # Apple-system-Python guard runs BEFORE any extension-loading attempt
    # so the failure surface is one clear :class:`RuntimeError` with the
    # install hint. If we deferred this to the stdlib's actual
    # ``enable_load_extension`` call, the resulting :class:`AttributeError`
    # would not match the :class:`OperationalError` clause and the apsw
    # fallback would not fire — but the user's real problem is "system
    # Python without extensions", so falling through to apsw would mask
    # the diagnostic. Raise loudly here instead.
    _check_sqlite_extension_loading()

    path_str = str(path) if isinstance(path, Path) else path

    try:
        # ``check_same_thread=False`` matches :func:`open_lcm_db` — the
        # registry permits cross-thread close in test fixtures.
        conn = sqlite3.connect(path_str, check_same_thread=False)
    except sqlite3.OperationalError:
        # Connection-open itself failed (e.g. read-only mount). Try apsw
        # if available; otherwise re-raise so the operator sees the real
        # error rather than a swallowed one.
        if HAS_APSW:
            return _open_with_apsw(path, role)
        raise

    try:
        # Load sqlite-vec via the public helper. We pass ``silent=False``
        # so an operator sees the underlying cause logged at WARNING when
        # the load fails — useful diagnostic context for the "no apsw
        # available" branch below.
        loaded = try_load_sqlite_vec(conn, silent=False)
        if not loaded:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 -- cleanup
                pass
            if HAS_APSW:
                return _open_with_apsw(path, role)
            # No apsw extra installed AND stdlib load failed. Synthesize a
            # clear :class:`OperationalError` so the operator can choose:
            # install the apsw extra, or fix the interpreter's sqlite-vec
            # setup. The WARNING log above carries the original cause.
            raise sqlite3.OperationalError(
                "lossless-hermes: failed to load sqlite-vec via stdlib "
                "sqlite3 and the [apsw] extra is not installed. Install "
                "the apsw fallback with `pip install lossless-hermes[apsw]` "
                "or investigate why sqlite_vec.load() failed on this "
                "interpreter (see WARNING log above)."
            )

        # PRAGMA tunings. Mirror :func:`_configure_connection` order +
        # values, but override ``busy_timeout`` with the role-specific
        # value AFTER the helper would otherwise set the gateway default.
        # We don't reuse ``_configure_connection`` directly because the
        # busy_timeout override needs to happen INSIDE the configure flow
        # (or right after) — keeping the PRAGMA block local here makes the
        # role-override unambiguous.
        canonical_path = normalize_path(path)
        _apply_wal_with_fallback(conn, db_label=canonical_path)
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout}")
        conn.execute("PRAGMA foreign_keys = ON")
        assert_foreign_keys_enabled(conn)
        conn.execute("PRAGMA cache_size = -65536")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
    except Exception:
        # Half-configured connection: close before propagating.
        try:
            conn.close()
        except Exception:  # noqa: BLE001 -- cleanup
            pass
        raise

    _track_connection(path, conn)
    return conn


# Suppress the "unused" warning for HAS_APSW + APPLE_SYSTEM_PYTHON_MSG +
# _has_sqlite_extension_loading — they are re-exported for test use.
_ = APPLE_SYSTEM_PYTHON_MSG
_ = _has_sqlite_extension_loading
