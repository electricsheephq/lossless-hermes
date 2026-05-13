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
from typing import TYPE_CHECKING, Literal, Set, Tuple, Union

import sqlite_vec

from lossless_hermes.engine import _check_sqlite_extension_loading

if TYPE_CHECKING:
    pass

__all__ = [
    "SQLITE_BUSY_TIMEOUT_MS",
    "assert_foreign_keys_enabled",
    "close_lcm_connection",
    "close_lcm_db",
    "get_file_backed_database_path",
    "is_in_memory_path",
    "normalize_path",
    "open_lcm_db",
]

_log = logging.getLogger("lossless_hermes.db.connection")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Matches ``connection.ts`` line 12 (``SQLITE_BUSY_TIMEOUT_MS = 30_000``).
# 30 s accommodates high-concurrency multi-agent setups where ≥10 writers
# contend on the WAL. The 5 s default proved insufficient in production —
# see ``storage.md`` §3 table and the comment in ``connection.ts:8-11``.
SQLITE_BUSY_TIMEOUT_MS = 30_000

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
