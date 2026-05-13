"""One-time vec0 + embedding-profile bootstrap (issue 08-14).

Ports ``lossless-claw/src/operator/semantic-infra-init.ts`` (LCM commit
``1f07fbd`` on branch ``pr-613``, 196 LOC) to Python. Fires once at
plugin load (per ``docs/porting-guides/plugin-glue.md`` "Plugin
registration sequence", implicit in engine init) so the autostart's
pre-flight checks actually pass in production.

Without this module, ``runSemanticSearch`` / ``runHybridSearch`` would
raise :class:`SemanticSearchUnavailableError` on every call: vec0 is
loaded by :func:`open_lcm_db`, but the per-model
``lcm_embeddings_<slug>`` virtual table + the suppression cascade
triggers must be created before the backfill cron writes its first
embedding. This module wires that bootstrap.

### Behavior (mirrors TS source)

1. **Load vec0** — best-effort via
   :func:`lossless_hermes.db.connection.try_load_sqlite_vec`. The
   extension is normally pre-loaded by :func:`open_lcm_db`; we re-attempt
   here so callers that opened the connection themselves still get a
   bootstrap. If the load fails (sqlite-vec not installed, Apple system
   Python without ``--enable-loadable-sqlite-extensions``), we return
   ``kind="unavailable"`` with a clear reason — never raise.
2. **Register the embedding profile** — INSERT-IGNORE'd via
   :func:`lossless_hermes.embeddings.store.register_embedding_profile`.
   Idempotent on ``(model_name, dim)`` match; second call is a no-op.
3. **Create the per-model vec0 virtual table** — via
   :func:`lossless_hermes.embeddings.store.ensure_embeddings_table`.
   Schema: ``embedding float[<dim>], +embedded_id text, embedded_kind
   text, suppressed integer``. Idempotent — ``CREATE VIRTUAL TABLE IF
   NOT EXISTS``.
4. **Create the suppression triggers** — ``lcm_embed_suppress_<slug>``
   (AFTER UPDATE OF ``suppressed_at`` ON ``summaries``) +
   ``lcm_embed_delete_<slug>`` (AFTER DELETE ON ``summaries``). Same
   ``ensure_embeddings_table`` call creates them. Per
   ``docs/porting-guides/doctor-ops.md`` §"Schema additions to support
   suppression" line 301, these triggers maintain the ``suppressed``
   metadata column on the vec0 table so semantic search filters at
   query time without joining.

### Idempotency (load-bearing)

The TS source uses ``INSERT OR IGNORE``, ``CREATE VIRTUAL TABLE IF NOT
EXISTS``, ``CREATE TRIGGER IF NOT EXISTS`` — every step is idempotent.
The Python port keeps this contract and also reports the difference
to the caller: a first-call result reports the triggers that were
created (``kind="initialized"``); a second-call result reports
``kind="already_initialized"`` with ``triggers_created=[]``. The
distinction is observable via ``sqlite_master`` — we pre-check for the
trigger names and report only the ones that didn't exist before the
call.

### Voyage-vs-other-embedder caveat

Per ``docs/porting-guides/doctor-ops.md`` line 313: "DROP? Yes if
Hermes uses pgvector/Qdrant/other". The v0.1 port assumes sqlite-vec
(matching the TS source and Epic 05's stance per ADR-006). If a future
ADR swaps the vector store, this module is replaced wholesale, not
refactored.

### Configuration (env-var driven, parity with TS)

* ``LCM_EMBEDDING_MODEL`` — default ``voyage-4-large`` (matches
  ``semantic-infra-init.ts:53`` ``DEFAULT_MODEL``).
* ``LCM_EMBEDDING_DIM`` — default ``1024`` (matches
  ``semantic-infra-init.ts:54`` ``DEFAULT_DIM``). When the resolved
  model has a known dim and ``LCM_EMBEDDING_DIM`` is not set, the known
  dim wins (so ``voyage-3-lite`` defaults to 512 not 1024).
* ``LCM_DISABLE_SEMANTIC=true`` — opt-out. Returns ``kind="unavailable"``
  with reason ``"LCM_DISABLE_SEMANTIC=true"`` without touching the DB.

The full ``HERMES_*`` env-var aliases (per ``docs/porting-guides/
tests-and-config.md`` §"Env-var aliases") are NOT honored by this
module's defaults; we intentionally keep the env-var surface minimal so
the bootstrap is reproducible. The ``deps`` parameter lets callers
inject model/dim from a typed config object (the recommended path for
Hermes integration once Epic 01-02 config resolution lands).

See:

* ``epics/08-cli-ops/08-14-semantic-infra-init.md`` — this issue.
* ``lossless-claw/src/operator/semantic-infra-init.ts:1-196`` — TS
  source pinned at commit ``1f07fbd``.
* ``docs/porting-guides/doctor-ops.md`` §"Operator modules" line 313.
"""

from __future__ import annotations

import logging
import os
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from lossless_hermes.db.connection import (
    Connection,
    try_load_sqlite_vec,
    vec0_version,
)
from lossless_hermes.embeddings.store import (
    _slug_for,
    embeddings_table_name,
    ensure_embeddings_table,
    register_embedding_profile,
)

_log = logging.getLogger("lossless_hermes.operator.semantic_infra")

__all__ = [
    "DEFAULT_DIM",
    "DEFAULT_MODEL",
    "KNOWN_MODEL_DIMS",
    "SemanticInfraDeps",
    "SemanticInfraInitResult",
    "init_semantic_infra_if_possible",
]


# ---------------------------------------------------------------------------
# Constants (port of semantic-infra-init.ts:53-63)
# ---------------------------------------------------------------------------

# Default embedding model — matches TS ``semantic-infra-init.ts:53``.
DEFAULT_MODEL: str = "voyage-4-large"

# Default embedding dim — matches TS ``semantic-infra-init.ts:54``.
# ``voyage-4-large`` ships 1024-dim vectors.
DEFAULT_DIM: int = 1024

# Known model -> dim mappings to sanity-check operator config.
# Mirrors TS ``semantic-infra-init.ts:56-63`` ``KNOWN_MODEL_DIMS``. Used
# to (a) resolve dim when only ``LCM_EMBEDDING_MODEL`` is set, (b) warn
# on dim/model mismatch when the operator provides an inconsistent pair.
KNOWN_MODEL_DIMS: dict[str, int] = {
    "voyage-4-large": 1024,
    "voyage-3": 1024,
    "voyage-3-large": 1024,
    "voyage-3-lite": 512,
    "voyage-code-3": 1024,
}


# ---------------------------------------------------------------------------
# Public typed surfaces
# ---------------------------------------------------------------------------


class SemanticInfraDeps(BaseModel):
    """Caller-injected configuration for :func:`init_semantic_infra_if_possible`.

    Provides a typed way to override the env-var defaults — typically
    used by tests + the Hermes config resolver once Epic 01-02 lands.
    When :data:`None` is passed for any field, the env var is consulted;
    when both are unset, the hardcoded default fires.

    Precedence (matches the TS source's ``opts.env ?? process.env``
    pattern via the dedicated ``env`` field): **deps field > env var >
    hardcoded default**.

    Attributes:
        model_name: Embedding model identifier (e.g. ``"voyage-4-large"``).
            When :data:`None`, reads ``LCM_EMBEDDING_MODEL`` from
            ``env``, falling back to :data:`DEFAULT_MODEL`.
        dim: Vector dimension. When :data:`None`, reads
            ``LCM_EMBEDDING_DIM`` from ``env``, then
            :data:`KNOWN_MODEL_DIMS` lookup for the resolved model,
            falling back to :data:`DEFAULT_DIM`.
        env: Environment-variable :class:`Mapping`. Defaults to
            :data:`os.environ`. Tests pass an isolated :class:`dict` to
            avoid leaking process env into the DB.
        disable_semantic: When :data:`True`, returns
            ``kind="unavailable"`` with reason
            ``"LCM_DISABLE_SEMANTIC=true"`` without touching the DB.
            When :data:`None`, reads ``LCM_DISABLE_SEMANTIC`` from
            ``env`` and treats ``"true"`` (case-insensitive, trimmed) as
            opt-out.
    """

    # Mapping is not directly serializable by pydantic v2; we mark the
    # model as ``arbitrary_types_allowed`` because we never need to
    # serialize this object — it's a pure DI container consumed at the
    # call site.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_name: str | None = None
    dim: int | None = None
    env: Mapping[str, str] | None = None
    disable_semantic: bool | None = None


class SemanticInfraInitResult(BaseModel):
    """Result of :func:`init_semantic_infra_if_possible`.

    Tri-state ``kind`` matches the issue spec:

    * ``"initialized"`` — first-call result. The profile was newly
      registered and/or the vec0 table was newly created. At least one
      of ``triggers_created`` is non-empty (the AFTER UPDATE / AFTER
      DELETE pair).
    * ``"already_initialized"`` — subsequent-call result. Everything
      was already in place; ``triggers_created`` is empty.
    * ``"unavailable"`` — vec0 not loadable, ``LCM_DISABLE_SEMANTIC=true``,
      or another non-fatal blocker. ``reason`` describes the cause.
      ``profile_id``, ``table_name``, ``triggers_created`` are all
      :data:`None` / empty.

    Attributes:
        kind: Tri-state result classifier.
        profile_id: When ``kind != "unavailable"``, the model name that
            was registered (matches ``lcm_embedding_profile.model_name``
            column). :data:`None` on ``unavailable``.
        table_name: When ``kind != "unavailable"``, the vec0 table name
            (e.g. ``"lcm_embeddings_voyage4large"``).
        triggers_created: List of trigger names that this call actually
            CREATEd (i.e. didn't already exist). Empty on
            ``already_initialized``.
        reason: Diagnostic message for ``kind="unavailable"`` callers.
            :data:`None` otherwise.
    """

    kind: Literal["initialized", "already_initialized", "unavailable"]
    profile_id: str | None = None
    table_name: str | None = None
    triggers_created: list[str] = Field(default_factory=list)
    reason: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_env(deps: SemanticInfraDeps | None) -> Mapping[str, str]:
    """Return the env mapping to consult — caller-supplied or :data:`os.environ`."""
    if deps is not None and deps.env is not None:
        return deps.env
    return os.environ


def _resolve_disable_semantic(
    deps: SemanticInfraDeps | None, env: Mapping[str, str]
) -> tuple[bool, str | None]:
    """Resolve the ``LCM_DISABLE_SEMANTIC`` opt-out.

    Returns ``(disabled, source)`` where ``source`` is a short human
    string describing where the opt-out came from (for the
    ``unavailable`` reason). When not disabled, ``source`` is :data:`None`.
    """
    if deps is not None and deps.disable_semantic is True:
        return True, "deps.disable_semantic=True"
    if deps is not None and deps.disable_semantic is False:
        # Explicit override — bypass env-var entirely.
        return False, None
    raw = env.get("LCM_DISABLE_SEMANTIC", "").strip().lower()
    if raw == "true":
        return True, "LCM_DISABLE_SEMANTIC=true"
    return False, None


def _resolve_model_name(deps: SemanticInfraDeps | None, env: Mapping[str, str]) -> str:
    """Resolve the embedding model name.

    Precedence: ``deps.model_name`` > ``LCM_EMBEDDING_MODEL`` env var >
    :data:`DEFAULT_MODEL`.

    Empty / whitespace-only values fall through to the next tier
    (matches TS ``semantic-infra-init.ts:122`` ``env.LCM_EMBEDDING_MODEL?.trim() || DEFAULT_MODEL``).
    """
    if deps is not None and deps.model_name is not None:
        stripped = deps.model_name.strip()
        if stripped:
            return stripped
    raw = env.get("LCM_EMBEDDING_MODEL", "").strip()
    if raw:
        return raw
    return DEFAULT_MODEL


def _resolve_dim(
    deps: SemanticInfraDeps | None,
    env: Mapping[str, str],
    model_name: str,
) -> int:
    """Resolve the embedding dim.

    Precedence: ``deps.dim`` > ``LCM_EMBEDDING_DIM`` env var >
    :data:`KNOWN_MODEL_DIMS` lookup > :data:`DEFAULT_DIM`.

    Non-positive or non-integer env values fall through (with a warning
    log) to the next tier — matches TS ``semantic-infra-init.ts:125-137``.
    """
    if deps is not None and deps.dim is not None:
        return deps.dim
    raw = env.get("LCM_EMBEDDING_DIM", "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
        # Non-positive or non-integer: warn + fall through.
        _log.warning(
            "[semantic_infra] LCM_EMBEDDING_DIM=%r is not a positive integer; "
            "falling back to known-model default for %s",
            raw,
            model_name,
        )
    return KNOWN_MODEL_DIMS.get(model_name, DEFAULT_DIM)


def _trigger_exists(conn: Connection, name: str) -> bool:
    """Does a SQLite trigger named ``name`` exist?

    Cheap ``sqlite_master`` probe. Used to compute the
    ``triggers_created`` delta — we record the pre-state, call
    :func:`ensure_embeddings_table` (idempotent CREATE IF NOT EXISTS),
    then any trigger that wasn't there before counts as "newly created".
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _profile_exists(conn: Connection, model_name: str) -> bool:
    """Is ``model_name`` already registered in ``lcm_embedding_profile``?

    Used to compute the ``initialized`` vs ``already_initialized``
    distinction — registering a profile is the first observable side
    effect, so its prior absence is the strong "first call" signal.
    """
    row = conn.execute(
        "SELECT 1 FROM lcm_embedding_profile WHERE model_name = ?",
        (model_name,),
    ).fetchone()
    return row is not None


def _table_exists(conn: Connection, table_name: str) -> bool:
    """Cheap ``sqlite_master`` probe for a vec0 virtual table."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def init_semantic_infra_if_possible(
    conn: Connection,
    deps: SemanticInfraDeps | None = None,
) -> SemanticInfraInitResult:
    """Best-effort vec0 + embedding-profile bootstrap.

    Ports ``lossless-claw/src/operator/semantic-infra-init.ts:74-196``
    ``initSemanticInfraIfPossible``. Returns a typed result snapshot
    rather than the TS-shaped flag bag so callers can pattern-match on
    ``kind``.

    Idempotent — safe to call on every plugin reload. The
    ``triggers_created`` list reflects only the trigger names that this
    specific call CREATEd (i.e. didn't already exist). On a freshly
    initialized DB the list contains both ``lcm_embed_suppress_<slug>``
    and ``lcm_embed_delete_<slug>``; on a second call it's empty.

    Args:
        conn: An open SQLite connection. The migrations must already
            have created ``lcm_embedding_profile`` and ``summaries``
            (callers receive this via :func:`open_lcm_db`).
        deps: Optional caller-injected config. When :data:`None`, all
            settings come from the process environment +
            :data:`KNOWN_MODEL_DIMS` defaults.

    Returns:
        A :class:`SemanticInfraInitResult`. Never raises on the
        vec0-missing path; raises only when the DB connection itself is
        in a bad state (e.g. ``lcm_embedding_profile`` table missing —
        caller forgot to run migrations).

    Raises:
        sqlite3.OperationalError: ``lcm_embedding_profile`` table
            doesn't exist (migrations not run); ``summaries`` table
            doesn't exist (migrations not run).
    """
    env = _resolve_env(deps)

    # Step 0: short-circuit on LCM_DISABLE_SEMANTIC=true. Matches TS
    # ``semantic-infra-init.ts:81-91``.
    disabled, disable_source = _resolve_disable_semantic(deps, env)
    if disabled:
        _log.info("[semantic_infra] disabled via %s", disable_source)
        return SemanticInfraInitResult(
            kind="unavailable",
            reason=disable_source,
        )

    # Step 1: load vec0 (best-effort). Matches TS
    # ``semantic-infra-init.ts:93-117``. We use ``silent=True`` because
    # we synthesize our own warning with a more actionable message below.
    try:
        loaded = try_load_sqlite_vec(conn, silent=True)
    except ImportError as exc:
        # ``sqlite_vec`` package itself not installed. The
        # ``try_load_sqlite_vec`` shim doesn't catch ImportError because
        # it imports sqlite_vec at module-level; if we're called from
        # a context where the import has been monkey-patched to raise
        # (the typical mock setup for the unavailable test path), we
        # catch it here and return ``unavailable`` rather than letting
        # it propagate.
        reason = f"sqlite-vec import failed: {exc}"
        _log.warning(
            "[semantic_infra] sqlite-vec not loadable; semantic retrieval "
            "(lcm_grep --mode semantic, lcm_grep --mode hybrid) will be "
            "unavailable. Install via `pip install sqlite-vec`. Reason: %s",
            exc,
        )
        return SemanticInfraInitResult(kind="unavailable", reason=reason)

    version = vec0_version(conn)
    if not loaded or version is None:
        # LCM Wave-10 (2026-03-21): warn-level, not info-level, so operators
        # visibly see this at boot. Previously the lack of sqlite-vec was
        # silently logged at info-level, leaving operators to wonder why
        # ``lcm_grep --mode semantic`` and ``lcm_grep --mode hybrid``
        # returned degraded results despite ``VOYAGE_API_KEY`` being
        # configured. (Wave-12 SA: ``lcm_semantic_recall`` removed; semantic
        # surfaces are now both modes of ``lcm_grep``.)
        # Original: lossless-claw/src/operator/semantic-infra-init.ts:97-108.
        reason = "sqlite-vec not loadable"
        _log.warning(
            "[semantic_infra] sqlite-vec extension not loaded; semantic "
            "retrieval (lcm_grep --mode semantic, lcm_grep --mode hybrid) "
            "will be unavailable. Install via `pip install sqlite-vec` and "
            "restart the host."
        )
        return SemanticInfraInitResult(kind="unavailable", reason=reason)

    _log.info("[semantic_infra] sqlite-vec loaded (version=%s)", version)

    # Step 2: resolve model + dim from deps/env. Matches TS
    # ``semantic-infra-init.ts:121-147``.
    model_name = _resolve_model_name(deps, env)
    dim = _resolve_dim(deps, env, model_name)

    # Sanity-check: if the model has a known dim and the resolved dim
    # disagrees, warn but don't block (operator may have a custom
    # variant). Matches TS ``semantic-infra-init.ts:141-147``.
    expected_dim = KNOWN_MODEL_DIMS.get(model_name)
    if expected_dim is not None and expected_dim != dim:
        _log.warning(
            "[semantic_infra] dim=%d doesn't match known dim=%d for %s; "
            "proceeding (operator may have a custom variant), but verify "
            "embedding output dim matches",
            dim,
            expected_dim,
            model_name,
        )

    # Step 3: compute the pre-state so we can report what THIS call
    # actually changed. Record whether the profile + the two triggers
    # already existed BEFORE we run the idempotent ensure* calls. The
    # post-call delta is the ``triggers_created`` list + the
    # ``initialized`` vs ``already_initialized`` classifier.
    table_name = embeddings_table_name(model_name)
    slug = _slug_for(model_name)
    suppress_trigger = f"lcm_embed_suppress_{slug}"
    delete_trigger = f"lcm_embed_delete_{slug}"

    profile_was_present = _profile_exists(conn, model_name)
    table_was_present = _table_exists(conn, table_name)
    suppress_trigger_was_present = _trigger_exists(conn, suppress_trigger)
    delete_trigger_was_present = _trigger_exists(conn, delete_trigger)

    # Step 4: register the embedding profile (INSERT OR IGNORE,
    # idempotent). Matches TS ``semantic-infra-init.ts:151-166``. Errors
    # from ``register_embedding_profile`` (slug collision, dim mismatch)
    # surface as ``unavailable`` — operator must resolve before semantic
    # retrieval works.
    try:
        register_embedding_profile(conn, model_name, dim)
    except (ValueError, RuntimeError) as exc:
        reason = f"profile registration failed: {exc}"
        _log.warning(
            "[semantic_infra] profile registration failed: %s. Semantic "
            "retrieval will be unavailable until this is resolved.",
            exc,
        )
        return SemanticInfraInitResult(kind="unavailable", reason=reason)

    # Step 5: create the vec0 virtual table + the two suppression
    # cascade triggers (idempotent CREATE IF NOT EXISTS). Matches TS
    # ``semantic-infra-init.ts:169-184``.
    try:
        ensure_embeddings_table(conn, model_name, dim)
    except (ValueError, Exception) as exc:  # noqa: BLE001 -- spec: degrade on any error
        # ``ensure_embeddings_table`` will raise ``sqlite3.OperationalError``
        # if vec0 isn't actually usable on this connection (e.g. the
        # ``CREATE VIRTUAL TABLE USING vec0(...)`` statement fails).
        # Surface as ``unavailable`` rather than letting it crash plugin
        # init.
        reason = f"ensure_embeddings_table failed: {exc}"
        _log.warning(
            "[semantic_infra] ensure_embeddings_table failed: %s. Profile "
            "is registered but the vec0 table couldn't be created — "
            "semantic backfill will fail.",
            exc,
        )
        return SemanticInfraInitResult(
            kind="unavailable",
            profile_id=model_name,
            reason=reason,
        )

    # Step 6: compute the ``triggers_created`` delta + classify the
    # call.
    triggers_created: list[str] = []
    if not suppress_trigger_was_present:
        triggers_created.append(suppress_trigger)
    if not delete_trigger_was_present:
        triggers_created.append(delete_trigger)

    # A call is "initialized" if it created any side effect: registered
    # a new profile row, created the vec0 table, or created either
    # trigger. All four flags can flip on the same call (fresh DB) or
    # none can (second call on an already-bootstrapped DB).
    any_new_side_effect = not profile_was_present or not table_was_present or bool(triggers_created)
    kind: Literal["initialized", "already_initialized"] = (
        "initialized" if any_new_side_effect else "already_initialized"
    )

    _log.info(
        "[semantic_infra] %s (model=%s dim=%d, triggers_created=%s)",
        kind,
        model_name,
        dim,
        triggers_created,
    )
    return SemanticInfraInitResult(
        kind=kind,
        profile_id=model_name,
        table_name=table_name,
        triggers_created=triggers_created,
    )
