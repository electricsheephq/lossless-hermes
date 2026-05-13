"""Lossless-hermes operator config (full port of ``src/db/config.ts``).

Ports the LCM ``LcmConfig`` shape and ``resolveLcmConfig`` resolver from
TypeScript (commit ``1f07fbd`` of upstream lossless-claw, file
``src/db/config.ts``, 630 LOC) to Python with pydantic v2. The TS
architecture is preserved 1:1:

* Typed model (:class:`LcmConfig`) with snake_case field names matching
  the TS camelCase 1:1 (e.g. ``contextThreshold`` → ``context_threshold``).
* Pure-function resolver :func:`resolve_lcm_config` that takes
  ``(env, plugin_config)`` and returns a fully-defaulted ``LcmConfig``.
  Precedence (matches TS exactly): **env var > plugin config > hardcoded
  default**.
* Diagnostics tuple variant :func:`resolve_lcm_config_with_diagnostics`
  tracking pattern-array provenance for the ``hermes doctor`` /
  ``lcm doctor`` equivalent (per
  ``docs/porting-guides/tests-and-config.md`` §"Config diagnostics —
  port resolveLcmConfigWithDiagnostics").
* Hermes-side YAML loader :func:`load_config` reading
  ``$HERMES_HOME/config.yaml`` (top-level ``lossless_hermes:`` namespace
  per ADR-023) and feeding the subtree through the resolver. Unknown
  keys raise :class:`pydantic.ValidationError` via ``extra='forbid'``
  (ADR-023 §Consequences).
* Voyage credential resolver :func:`resolve_voyage_api_key` implementing
  the three-tier order per ADR-022: **config inline > env >
  $HERMES_HOME file**.

### Field inventory

The full TS inventory (~52 in-scope ``LcmConfig`` fields + 67 env-var
overrides) is documented in
``docs/porting-guides/tests-and-config.md`` §"Configuration surface —
full inventory" (lines 233-410). Every field below cites the TS line in
``/Volumes/LEXAR/Claude/lossless-claw/src/db/config.ts``.

### Env-var aliases (Phase 1 policy)

Per ``tests-and-config.md`` line 556 and ADR for Hermes-side env-var
rename policy, the resolver accepts **both** ``LCM_*`` (legacy LCM/
OpenClaw) and ``HERMES_*`` (target Hermes) prefixes. ``LCM_*`` emits a
single :class:`DeprecationWarning` per env var per process (debounced
via :func:`functools.lru_cache`) so an operator migrating from LCM
gets a clear migration cue without log spam. The ``LCM_*`` prefix is
intended to drop in Phase 3 (future major version).

Resolution order (per env var):

1. ``HERMES_FOO`` (primary, no warning)
2. ``LCM_FOO`` (legacy alias; emits ``DeprecationWarning`` once)
3. Plugin config field
4. Hardcoded default

### Out of scope (other epics)

Per the issue 01-02 spec §"Env-var coverage", these env vars are
consumed directly by their owning subsystems (not ``LcmConfig``):

* ``LCM_DEFAULT_TOKEN_BUDGET`` — read in engine (Epic 03+)
* ``LCM_SQLITE_VEC_PATH`` — read by vec0 discovery (Epic 05)
* ``LCM_DISABLE_SEMANTIC`` — ops bypass (Epic 05)
* ``LCM_EMBEDDING_DIM`` / ``LCM_EMBEDDING_MODEL`` — Voyage (Epic 05)
* ``LCM_EXTRACTION_LLM_ENABLED`` — extraction (Epic 06)

Test-only env vars (``LCM_TEST_VEC0_PATH``, ``REAL_HOME``, ``HOME``,
``ANTHROPIC_API_KEY``) live in ``tests/conftest.py``.

### Hermes-specific deviations from TS

* ``transcript_gc_enabled`` is kept for back-compat (operators
  migrating LCM configs see no surprise) but documented as a no-op on
  Hermes (transcript GC was OpenClaw-specific).
* ``auto_rotate_session_files`` is similarly kept; rotation of
  Hermes-owned session files is the host's responsibility, not this
  plugin's.
* Default state dir is ``~/.hermes`` (not ``~/.openclaw``). When
  ``OPENCLAW_STATE_DIR`` is set (e.g. migration scenario), the
  resolver still respects it for database/large-files defaults — see
  :func:`resolve_openclaw_state_dir`. Hermes-side default uses
  :func:`resolve_hermes_state_dir`.

### Source-of-truth pointers

* TS reference: ``/Volumes/LEXAR/Claude/lossless-claw/src/db/config.ts``
  (commit ``1f07fbd``)
* TS test reference: ``test/config.test.ts`` (61 cases, this Python
  port mirrors each)
* Field inventory: ``docs/porting-guides/tests-and-config.md`` §"Config
  fields (from LcmConfig type — full inventory)"
* ADR-022 (Voyage credentials), ADR-023 (config delivery namespace),
  ADR-024 (file path), ADR-029 (Wave-N provenance — none in this file)
"""

from __future__ import annotations

import functools
import os
import re
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES",
    "DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO",
    "AutoRotateSessionFileMode",
    "AutoRotateSessionFilesConfig",
    "CacheAwareCompactionConfig",
    "DynamicLeafChunkTokensConfig",
    "FallbackProvider",
    "LcmConfig",
    "LcmConfigDiagnostics",
    "LcmConfigSource",
    "ProactiveThresholdCompactionMode",
    "WorkerConfig",
    "describe_lcm_config_source",
    "load_config",
    "resolve_hermes_state_dir",
    "resolve_lcm_config",
    "resolve_lcm_config_with_diagnostics",
    "resolve_openclaw_state_dir",
    "resolve_voyage_api_key",
]


# ---------------------------------------------------------------------------
# Constants (single source of truth — mirror TS exports)
# ---------------------------------------------------------------------------

# Mirror of TS ``DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO = 0.70`` (config.ts:26).
# Referenced by resolver fallback, runtime fallback, and tests so all three
# agree on the threshold.
DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO = 0.70

# Mirror of TS ``DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES = 2 * 1024 * 1024``
# (config.ts:27). 2 MiB.
DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# Type aliases (mirror TS string-literal unions)
# ---------------------------------------------------------------------------

ProactiveThresholdCompactionMode = Literal["deferred", "inline"]
AutoRotateSessionFileMode = Literal["rotate", "warn", "off"]
LcmConfigSource = Literal["env", "plugin-config", "default"]


# ---------------------------------------------------------------------------
# Env-var alias plumbing (Phase 1 deprecation policy)
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _emit_lcm_deprecation_warning(lcm_var: str, hermes_var: str) -> None:
    """Emit a single ``DeprecationWarning`` per ``LCM_*`` env var per process.

    Mirrors the Phase-1 rename policy from
    ``docs/porting-guides/tests-and-config.md`` line 556. The
    :func:`functools.lru_cache` debounces the warning — the value
    itself is still read every call; only the warning fires once.

    Note: tests can clear this cache with
    ``_emit_lcm_deprecation_warning.cache_clear()`` between cases.
    """
    warnings.warn(
        f"{lcm_var} is deprecated; use {hermes_var} instead. "
        f"LCM_* aliases will be removed in lossless-hermes 1.0.",
        DeprecationWarning,
        stacklevel=2,
    )


def _read_env(
    env: Mapping[str, str],
    *,
    hermes_name: str,
    lcm_name: str | None = None,
) -> str | None:
    """Read an env var honoring both ``HERMES_*`` (primary) and ``LCM_*``
    (legacy) prefixes.

    Returns the first non-``None`` value (NOT first non-empty — the TS
    treats ``""`` and "set" distinctly for some flags, e.g.
    ``LCM_ENABLED=""`` keeps default rather than equating to ``"false"``).
    When the value comes from the ``LCM_*`` alias, emits one
    ``DeprecationWarning`` per env var per process.

    If ``lcm_name`` is ``None``, no legacy alias is checked (used for
    Hermes-only knobs that never had an LCM equivalent).
    """
    hermes_val = env.get(hermes_name)
    if hermes_val is not None:
        return hermes_val
    if lcm_name is None:
        return None
    lcm_val = env.get(lcm_name)
    if lcm_val is not None:
        _emit_lcm_deprecation_warning(lcm_name, hermes_name)
        return lcm_val
    return None


# ---------------------------------------------------------------------------
# Coercion helpers (mirror TS toNumber / parseFiniteInt / toBool / etc.)
# ---------------------------------------------------------------------------


def _to_number(value: Any) -> float | None:
    """Coerce a value to a finite float, or ``None``.

    Mirrors TS ``toNumber`` (config.ts:193). Accepts ``int`` / ``float``
    / numeric-string; rejects ``bool`` (Python's ``bool`` is an ``int``
    subclass — guard explicitly so ``True``/``False`` don't become
    ``1.0``/``0.0`` and silently corrupt numeric fields).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if f == f and f not in (float("inf"), float("-inf")):  # NaN check
            return f
        return None
    if isinstance(value, str):
        try:
            f = float(value)
        except (ValueError, TypeError):
            return None
        if f == f and f not in (float("inf"), float("-inf")):
            return f
        return None
    return None


def _parse_finite_int(value: str | None) -> int | None:
    """Parse a finite ``int`` from a string, or ``None``.

    Mirrors TS ``parseFiniteInt`` (config.ts:204) — uses ``parseInt``
    semantics (parses leading digits; trailing garbage is allowed if a
    valid prefix exists). Python's ``int("12abc")`` raises, so we use
    a regex-based prefix extraction to match TS behavior. ``None`` and
    pure-garbage strings ⇒ ``None``.
    """
    if value is None:
        return None
    # Match TS parseInt: pull leading sign + digits, ignore the rest.
    m = re.match(r"^\s*([+-]?\d+)", value)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _parse_finite_number(value: str | None) -> float | None:
    """Parse a finite ``float`` from a string, or ``None``.

    Mirrors TS ``parseFiniteNumber`` (config.ts:211) — JS ``parseFloat``
    parses leading numeric prefix and discards the rest. Python's
    ``float()`` is strict, so we use a regex to extract the leading
    numeric run before delegating to ``float``.
    """
    if value is None:
        return None
    m = re.match(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", value)
    if m is None:
        return None
    try:
        f = float(m.group(1))
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN/inf guard
        return None
    return f


def _to_bool(value: Any) -> bool | None:
    """Coerce a value to a boolean, or ``None``.

    Mirrors TS ``toBool`` (config.ts:251). Note: Python's
    ``bool`` is an ``int`` subclass, so we check it BEFORE other
    numeric paths. Only the exact strings ``"true"`` / ``"false"`` (no
    casing/whitespace handling — matches TS strict semantics).
    """
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _to_str(value: Any) -> str | None:
    """Coerce a value to a trimmed non-empty string, or ``None``.

    Mirrors TS ``toStr`` (config.ts:259). Returns ``None`` for
    empty/whitespace-only strings AND for non-string inputs.
    """
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed if trimmed else None
    return None


def _to_str_array(value: Any) -> list[str] | None:
    """Coerce plugin-config value to a trimmed string list, or ``None``.

    Mirrors TS ``toStrArray`` (config.ts:294). Behaviors:

    * If ``value`` is a list: trim each entry; drop empty entries;
      return ``[]`` if all empties (NOT ``None`` — keeps "explicit empty
      list" distinguishable from "missing"). TS returns ``[]`` here.
    * If ``value`` is a non-empty string: split on ``,``, trim each
      part, drop empty parts.
    * Otherwise: ``None``.
    """
    if isinstance(value, list):
        normalized = [t for entry in value if (t := _to_str(entry)) is not None]
        return normalized  # may be empty list — TS returns [] here too
    single = _to_str(value)
    if single is None:
        return None
    return [part.strip() for part in single.split(",") if part.strip()]


def _to_record(value: Any) -> dict[str, Any] | None:
    """Coerce to a dict, or ``None``.

    Mirrors TS ``toRecord`` (config.ts:311) — only plain objects, not
    arrays. Pydantic's nested models will see this and validate further.
    """
    if isinstance(value, dict):
        return value
    return None


def _parse_env_str_array(value: str | None) -> list[str] | None:
    """Parse a comma-separated env-var string into a trimmed list, or
    ``None``.

    Mirrors TS ``parseEnvStrArray`` (config.ts:317). ``None`` ⇒ ``None``
    (env var not set); empty/whitespace ⇒ ``[]`` (env var explicitly
    empty); else trimmed non-empty entries.
    """
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _to_proactive_mode(value: Any) -> ProactiveThresholdCompactionMode | None:
    """Coerce to ``"deferred" | "inline"``, or ``None``.

    Mirrors TS ``toProactiveThresholdCompactionMode`` (config.ts:267) —
    case-insensitive match after trimming.
    """
    normalized = _to_str(value)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if lowered in ("inline", "deferred"):
        return lowered  # type: ignore[return-value]
    return None


def _to_auto_rotate_mode(value: Any) -> AutoRotateSessionFileMode | None:
    """Coerce to ``"rotate" | "warn" | "off"``, or ``None``.

    Mirrors TS ``toAutoRotateSessionFileMode`` (config.ts:277).
    """
    normalized = _to_str(value)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if lowered in ("rotate", "warn", "off"):
        return lowered  # type: ignore[return-value]
    return None


def _to_positive_int(value: float | None) -> int | None:
    """Coerce to a positive integer (floor; min 1), or ``None``.

    Mirrors TS ``toPositiveInteger`` (config.ts:286).
    """
    if value is None:
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return max(1, int(value))


def _parse_fallback_providers_env(value: str | None) -> list[dict[str, str]] | None:
    """Parse env-format fallback providers ``"a/b,c/d"`` → list of dicts.

    Mirrors TS ``parseFallbackProviders`` (config.ts:218). Empty/
    whitespace-only ⇒ ``None`` (TS returns ``undefined``). Returns
    list of ``{"provider": ..., "model": ...}`` dicts; empty list
    after parsing ⇒ ``None``.
    """
    if value is None or not value.strip():
        return None
    entries: list[dict[str, str]] = []
    for part in value.split(","):
        trimmed = part.strip()
        if not trimmed:
            continue
        slash_idx = trimmed.find("/")
        if 0 < slash_idx < len(trimmed) - 1:
            provider = trimmed[:slash_idx].strip()
            model = trimmed[slash_idx + 1 :].strip()
            if provider and model:
                entries.append({"provider": provider, "model": model})
    return entries if entries else None


def _to_fallback_provider_array(value: Any) -> list[dict[str, str]] | None:
    """Convert plugin-config fallback providers array to list of dicts.

    Mirrors TS ``toFallbackProviderArray`` (config.ts:237). Filters to
    object-shaped entries with both ``provider`` and ``model``
    truthy strings.
    """
    if not isinstance(value, list):
        return None
    entries: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            p = _to_str(item.get("provider"))
            m = _to_str(item.get("model"))
            if p and m:
                entries.append({"provider": p, "model": m})
    return entries if entries else None


# ---------------------------------------------------------------------------
# Pydantic v2 models — nested objects (mirror TS sub-types)
# ---------------------------------------------------------------------------


class CacheAwareCompactionConfig(BaseModel):
    """Cache-sensitive policy for incremental leaf compaction.

    Mirrors TS ``CacheAwareCompactionConfig`` (config.ts:29-56).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    cache_ttl_seconds: int = Field(default=300, ge=1)
    max_cold_cache_catchup_passes: int = Field(default=2, ge=1)
    hot_cache_pressure_factor: float = Field(default=4.0, ge=1.0)
    hot_cache_budget_headroom_ratio: float = Field(default=0.2, ge=0.0, le=0.95)
    cold_cache_observation_threshold: int = Field(default=3, ge=1)
    # Per TS comment (config.ts:36-55), critical_budget_pressure_ratio is
    # optional in the input but always populated to a finite float in the
    # resolved config (defaults to 0.70). Clamped to [0, 1].
    critical_budget_pressure_ratio: float = Field(
        default=DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO,
        ge=0.0,
        le=1.0,
    )


class DynamicLeafChunkTokensConfig(BaseModel):
    """Dynamic step-band policy for incremental leaf chunk sizing.

    Mirrors TS ``DynamicLeafChunkTokensConfig`` (config.ts:58-61).
    Default ``max`` floor of ``leaf_chunk_tokens`` is enforced by the
    resolver (the model alone can't see ``leaf_chunk_tokens``).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max: int = Field(default=40000, ge=1)


class AutoRotateSessionFilesConfig(BaseModel):
    """Auto-rotation policy for session JSONL files.

    Mirrors TS ``AutoRotateSessionFilesConfig`` (config.ts:66-71).

    Note: rotation is OpenClaw-specific in the source. Kept for v0.1
    back-compat; documented as a no-op on Hermes (host owns session
    rotation policy, not this plugin).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    size_bytes: int = Field(
        default=DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES,
        ge=1,
    )
    startup: AutoRotateSessionFileMode = "rotate"
    runtime: AutoRotateSessionFileMode = "rotate"


class FallbackProvider(BaseModel):
    """A single provider/model pair for compaction-summarization fallback.

    Mirrors TS ``Array<{ provider: string; model: string }>``
    (config.ts:185, schema config.ts:396).
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str


class WorkerConfig(BaseModel):
    """Per-worker configuration (placeholder for Epic 02).

    Empty for v0; lands when the worker-loop dispatcher ports
    (Epic 02, ADR-020). See the original v0 skeleton docstring for
    rationale.
    """

    model_config = ConfigDict(extra="forbid")


class LcmConfigDiagnostics(BaseModel):
    """Provenance tracker for pattern-array fields.

    Mirrors TS ``LcmConfigDiagnostics`` (config.ts:75-80). Returned
    alongside the resolved config from
    :func:`resolve_lcm_config_with_diagnostics`. The ``hermes doctor``
    / ``lcm doctor`` equivalent reads this to tell operators "this
    value came from ENV / plugin-config / default" so they know what
    they're configuring.
    """

    model_config = ConfigDict(extra="forbid")

    ignore_session_patterns_source: LcmConfigSource
    stateless_session_patterns_source: LcmConfigSource
    ignore_session_patterns_env_overrides_config: bool
    stateless_session_patterns_env_overrides_config: bool


# ---------------------------------------------------------------------------
# LcmConfig — top-level pydantic model
# ---------------------------------------------------------------------------


class LcmConfig(BaseModel):
    """Lossless context-management operator config (full port).

    Mirrors TS ``LcmConfig`` (config.ts:82-190) field-for-field with
    TS camelCase → Python snake_case. Every default below matches the
    TS default at the resolver site (config.ts:448-606).

    ``model_config = ConfigDict(extra='forbid')`` is load-bearing —
    an operator who writes ``lossless_hermes: {threshhold: 0.7}``
    (typo) gets a clear startup error instead of silently running
    with the default. ADR-023 §Consequences pins this contract.

    Most fields support an ``HERMES_*`` env-var alias (and a legacy
    ``LCM_*`` alias that emits a ``DeprecationWarning`` once per
    process — see :func:`_read_env`). The alias plumbing lives in the
    resolver functions (:func:`resolve_lcm_config_with_diagnostics`),
    not on the field itself, because env-var precedence interacts with
    plugin-config-derived defaults (e.g. ``bootstrap_max_tokens``
    falls back to a function of ``leaf_chunk_tokens``).

    The fields use Pydantic ``Field`` with ``validation_alias`` only
    for the TS-source aliases like ``db_path`` (an old name for
    ``database_path``) and ``large_file_threshold_tokens`` (old name
    for ``large_file_token_threshold``). YAML/dict input can use
    either name; output (``model_dump()``) always uses the canonical
    snake_case.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # --- Top-level scalars ---
    enabled: bool = True
    database_path: str = Field(
        default="",  # resolver fills in
        validation_alias=AliasChoices("database_path", "db_path"),
    )
    large_files_dir: str = ""  # resolver fills in
    ignore_session_patterns: list[str] = Field(default_factory=list)
    stateless_session_patterns: list[str] = Field(default_factory=list)
    skip_stateless_sessions: bool = True
    context_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    fresh_tail_count: int = Field(default=64, ge=1)
    fresh_tail_max_tokens: int | None = Field(default=None, ge=0)
    prompt_aware_eviction: bool = False
    new_session_retain_depth: int = Field(default=2, ge=-1)
    leaf_min_fanout: int = Field(default=8, ge=2)
    condensed_min_fanout: int = Field(default=4, ge=2)
    condensed_min_fanout_hard: int = Field(default=2, ge=2)
    incremental_max_depth: int = Field(default=1, ge=-1)
    leaf_chunk_tokens: int = Field(default=20000, ge=1)
    bootstrap_max_tokens: int | None = Field(default=None, ge=1)
    leaf_target_tokens: int = Field(default=4000, ge=1)
    condensed_target_tokens: int = Field(default=2000, ge=1)
    max_expand_tokens: int = Field(default=4000, ge=1)
    large_file_token_threshold: int = Field(
        default=25000,
        ge=1000,
        validation_alias=AliasChoices(
            "large_file_token_threshold",
            "large_file_threshold_tokens",
        ),
    )

    # --- Provider/model overrides ---
    summary_provider: str = ""
    summary_model: str = ""
    large_file_summary_provider: str = ""
    large_file_summary_model: str = ""
    expansion_provider: str = ""
    expansion_model: str = ""
    delegation_timeout_ms: int = Field(default=120000, ge=1)
    summary_timeout_ms: int = Field(default=60000, ge=1)
    timezone: str = ""  # resolver fills in from TZ env / system default

    # --- Behavior flags ---
    prune_heartbeat_ok: bool = False
    transcript_gc_enabled: bool = False  # no-op on Hermes (kept for back-compat)
    agent_compaction_tool_enabled: bool = False
    proactive_threshold_compaction_mode: ProactiveThresholdCompactionMode = "deferred"

    # --- Nested objects ---
    auto_rotate_session_files: AutoRotateSessionFilesConfig = Field(
        default_factory=AutoRotateSessionFilesConfig,
    )

    # --- Optional token budgets ---
    max_assembly_token_budget: int | None = Field(default=None, ge=1000)
    tool_result_token_budget: int | None = Field(default=None, ge=2000)

    # --- Summarizer guard rails ---
    summary_max_overage_factor: float = Field(default=3.0, ge=1.0)
    custom_instructions: str = ""
    circuit_breaker_threshold: int = Field(default=5, ge=1)
    circuit_breaker_cooldown_ms: int = Field(default=1_800_000, ge=1)
    fallback_providers: list[FallbackProvider] = Field(default_factory=list)

    # --- Cache-aware compaction policy ---
    cache_aware_compaction: CacheAwareCompactionConfig = Field(
        default_factory=CacheAwareCompactionConfig,
    )
    dynamic_leaf_chunk_tokens: DynamicLeafChunkTokensConfig = Field(
        default_factory=DynamicLeafChunkTokensConfig,
    )

    # --- Voyage credentials (per ADR-022, tier-1 of the three-tier resolver) ---
    voyage_api_key: str | None = None

    # --- Worker placeholder (Epic 02) ---
    workers: dict[str, WorkerConfig] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# State-dir resolvers
# ---------------------------------------------------------------------------


def resolve_openclaw_state_dir(env: Mapping[str, str] | None = None) -> str:
    """Resolve the active OpenClaw state directory.

    Mirrors TS ``resolveOpenclawStateDir`` (config.ts:15-18).
    Precedence:

    1. ``OPENCLAW_STATE_DIR`` env var (after stripping whitespace)
    2. ``~/.openclaw`` (historic single-profile default)

    Kept on Hermes so the migration story (per ADR-003 §10.1) — read
    from openclaw-state-dir on first launch, write to hermes-state-dir
    — has a stable lookup helper.
    """
    if env is None:
        env = os.environ
    explicit = env.get("OPENCLAW_STATE_DIR", "").strip()
    if explicit:
        return explicit
    return str(Path.home() / ".openclaw")


def resolve_hermes_state_dir(env: Mapping[str, str] | None = None) -> str:
    """Resolve the active Hermes state directory.

    Hermes-side analog of :func:`resolve_openclaw_state_dir`.
    Precedence:

    1. ``HERMES_HOME`` env var (after stripping whitespace)
    2. ``~/.hermes`` (default)

    Used by :func:`load_config` to locate ``config.yaml`` and by
    :func:`resolve_voyage_api_key` for the credentials-file fallback.
    """
    if env is None:
        env = os.environ
    explicit = env.get("HERMES_HOME", "").strip()
    if explicit:
        return explicit
    return str(Path.home() / ".hermes")


def _pc_get(pc: Mapping[str, Any], *keys: str) -> Any:
    """Return ``pc[keys[i]]`` for the first ``keys[i]`` actually present.

    Mirrors TS plugin-config lookups that accept both camelCase and
    snake_case (e.g. ``pc.contextThreshold`` and ``pc.context_threshold``).
    Critical: uses ``in`` membership check, NOT truthiness. ``False`` /
    ``0`` / ``[]`` / ``""`` are valid values that should be returned —
    using ``pc.get(a) or pc.get(b)`` would silently swallow them.
    Returns ``None`` only if none of ``keys`` are in ``pc``.
    """
    for key in keys:
        if key in pc:
            return pc[key]
    return None


# ---------------------------------------------------------------------------
# Pattern-array resolver (helper for ignore/stateless session patterns)
# ---------------------------------------------------------------------------


def _resolve_pattern_array(
    *,
    env_value: str | None,
    plugin_value: Any,
) -> tuple[list[str], LcmConfigSource, bool]:
    """Resolve a pattern-array field with diagnostics.

    Mirrors TS ``resolvePatternArray`` (config.ts:327-358). Returns
    ``(patterns, source, env_overrides_plugin_config)``. When the env
    var is set (even to ``""`` → ``[]``), env wins and ``source="env"``.
    When env is unset but plugin-config has patterns, plugin wins.
    Otherwise defaults to ``[]`` with ``source="default"``.
    """
    plugin_patterns = _to_str_array(plugin_value)
    plugin_has_patterns = bool(plugin_patterns)  # non-empty list

    if env_value is not None:
        return (
            _parse_env_str_array(env_value) or [],
            "env",
            plugin_has_patterns,
        )
    if plugin_patterns is not None:
        return (plugin_patterns, "plugin-config", False)
    return ([], "default", False)


def describe_lcm_config_source(source: LcmConfigSource) -> str:
    """Human-friendly label for a config-source string.

    Mirrors TS ``describeLcmConfigSource`` (config.ts:360-369).
    Used by ``hermes doctor`` to render config provenance.
    """
    if source == "env":
        return "env"
    if source == "plugin-config":
        return "plugin config"
    return "defaults"


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------


def resolve_lcm_config_with_diagnostics(
    env: Mapping[str, str] | None = None,
    plugin_config: Mapping[str, Any] | None = None,
) -> tuple[LcmConfig, LcmConfigDiagnostics]:
    """Resolve ``LcmConfig`` from env + plugin-config with diagnostics.

    Mirrors TS ``resolveLcmConfigWithDiagnostics`` (config.ts:371-616)
    field-for-field. Precedence (per field): env > plugin-config >
    hardcoded default. Pattern-array diagnostics track whether env
    overrode a plugin-config value, exposed via the
    :class:`LcmConfigDiagnostics` return value.

    Env-var prefix resolution:

    * ``HERMES_FOO`` — primary, no warning.
    * ``LCM_FOO`` — legacy alias; emits one ``DeprecationWarning`` per
      env var per process (debounced).

    Both prefixes resolve to the same underlying value when set; if
    both are set, the ``HERMES_*`` value wins.

    Note: This function intentionally does NOT use ``pydantic-settings``'
    auto-env-loading because TS-equivalent precedence rules require
    explicit fall-through with type coercion that ``BaseSettings`` can't
    cleanly express (e.g. ``leaf_chunk_tokens`` participates in the
    derived default for ``bootstrap_max_tokens``). The resolver is a
    pure function; pydantic only handles final-shape validation.
    """
    if env is None:
        env = os.environ
    pc = dict(plugin_config or {})

    # Nested plugin-config records (may be None).
    cache_aware = _to_record(_pc_get(pc, "cacheAwareCompaction", "cache_aware_compaction"))
    dynamic_leaf = _to_record(_pc_get(pc, "dynamicLeafChunkTokens", "dynamic_leaf_chunk_tokens"))
    auto_rotate = _to_record(_pc_get(pc, "autoRotateSessionFiles", "auto_rotate_session_files"))

    # --- Derived defaults that depend on resolved leaf_chunk_tokens ---
    resolved_leaf_chunk_tokens = (
        _parse_finite_int(
            _read_env(env, hermes_name="HERMES_LEAF_CHUNK_TOKENS", lcm_name="LCM_LEAF_CHUNK_TOKENS")
        )
        or _to_number(_pc_get(pc, "leafChunkTokens", "leaf_chunk_tokens"))
        or 20000
    )
    resolved_leaf_chunk_tokens = int(resolved_leaf_chunk_tokens)

    # Bootstrap default = max(6000, leaf_chunk_tokens * 0.3).
    bootstrap_env = _parse_finite_int(
        _read_env(
            env, hermes_name="HERMES_BOOTSTRAP_MAX_TOKENS", lcm_name="LCM_BOOTSTRAP_MAX_TOKENS"
        )
    )
    bootstrap_pc = _to_number(_pc_get(pc, "bootstrapMaxTokens", "bootstrap_max_tokens"))
    if bootstrap_env is not None:
        resolved_bootstrap_max_tokens = bootstrap_env
    elif bootstrap_pc is not None:
        resolved_bootstrap_max_tokens = int(bootstrap_pc)
    else:
        resolved_bootstrap_max_tokens = max(
            6000,
            int(resolved_leaf_chunk_tokens * 0.3),
        )

    # Dynamic leaf chunk max — clamped to floor = leaf_chunk_tokens.
    dynamic_max_env = _parse_finite_int(
        _read_env(
            env,
            hermes_name="HERMES_DYNAMIC_LEAF_CHUNK_TOKENS_MAX",
            lcm_name="LCM_DYNAMIC_LEAF_CHUNK_TOKENS_MAX",
        )
    )
    dynamic_max_pc = _to_number((dynamic_leaf or {}).get("max"))
    dynamic_max_default = int(resolved_leaf_chunk_tokens * 2)
    resolved_dynamic_max = (
        dynamic_max_env
        if dynamic_max_env is not None
        else (int(dynamic_max_pc) if dynamic_max_pc is not None else dynamic_max_default)
    )
    resolved_dynamic_max = max(resolved_leaf_chunk_tokens, resolved_dynamic_max)

    # Hot-cache pressure factor — clamped to min 1.
    hot_pressure_env = _parse_finite_number(
        _read_env(
            env,
            hermes_name="HERMES_HOT_CACHE_PRESSURE_FACTOR",
            lcm_name="LCM_HOT_CACHE_PRESSURE_FACTOR",
        )
    )
    hot_pressure_pc = _to_number(
        _pc_get(cache_aware or {}, "hotCachePressureFactor", "hot_cache_pressure_factor"),
    )
    resolved_hot_pressure = max(
        1.0,
        hot_pressure_env
        if hot_pressure_env is not None
        else (hot_pressure_pc if hot_pressure_pc is not None else 4.0),
    )

    # Hot-cache budget headroom ratio — clamped to [0, 0.95].
    hot_headroom_env = _parse_finite_number(
        _read_env(
            env,
            hermes_name="HERMES_HOT_CACHE_BUDGET_HEADROOM_RATIO",
            lcm_name="LCM_HOT_CACHE_BUDGET_HEADROOM_RATIO",
        )
    )
    hot_headroom_pc = _to_number(
        _pc_get(
            cache_aware or {},
            "hotCacheBudgetHeadroomRatio",
            "hot_cache_budget_headroom_ratio",
        ),
    )
    resolved_hot_headroom = min(
        0.95,
        max(
            0.0,
            hot_headroom_env
            if hot_headroom_env is not None
            else (hot_headroom_pc if hot_headroom_pc is not None else 0.2),
        ),
    )

    # Cold-cache observation threshold — floor, min 1.
    cold_thresh_env = _parse_finite_number(
        _read_env(
            env,
            hermes_name="HERMES_COLD_CACHE_OBSERVATION_THRESHOLD",
            lcm_name="LCM_COLD_CACHE_OBSERVATION_THRESHOLD",
        )
    )
    cold_thresh_pc = _to_number(
        _pc_get(
            cache_aware or {},
            "coldCacheObservationThreshold",
            "cold_cache_observation_threshold",
        ),
    )
    resolved_cold_thresh = max(
        1,
        int(
            cold_thresh_env
            if cold_thresh_env is not None
            else (cold_thresh_pc if cold_thresh_pc is not None else 3),
        ),
    )

    # Critical budget pressure ratio — clamped to [0, 1].
    crit_env = _parse_finite_number(
        _read_env(
            env,
            hermes_name="HERMES_CRITICAL_BUDGET_PRESSURE_RATIO",
            lcm_name="LCM_CRITICAL_BUDGET_PRESSURE_RATIO",
        )
    )
    crit_pc = _to_number(
        _pc_get(
            cache_aware or {},
            "criticalBudgetPressureRatio",
            "critical_budget_pressure_ratio",
        ),
    )
    resolved_crit = min(
        1.0,
        max(
            0.0,
            crit_env
            if crit_env is not None
            else (crit_pc if crit_pc is not None else DEFAULT_CRITICAL_BUDGET_PRESSURE_RATIO),
        ),
    )

    # Auto-rotate session file size — positive int, default 2 MiB.
    auto_rotate_size_env = _to_positive_int(
        _parse_finite_int(
            _read_env(
                env,
                hermes_name="HERMES_AUTO_ROTATE_SESSION_FILES_SIZE_BYTES",
                lcm_name="LCM_AUTO_ROTATE_SESSION_FILES_SIZE_BYTES",
            )
        )
    )
    auto_rotate_size_pc = _to_positive_int(
        _to_number(_pc_get(auto_rotate or {}, "sizeBytes", "size_bytes"))
    )
    auto_rotate_size = (
        auto_rotate_size_env
        if auto_rotate_size_env is not None
        else (
            auto_rotate_size_pc
            if auto_rotate_size_pc is not None
            else DEFAULT_AUTO_ROTATE_SESSION_FILE_SIZE_BYTES
        )
    )

    # Pattern arrays (with diagnostics).
    ignore_env = _read_env(
        env,
        hermes_name="HERMES_IGNORE_SESSION_PATTERNS",
        lcm_name="LCM_IGNORE_SESSION_PATTERNS",
    )
    stateless_env = _read_env(
        env,
        hermes_name="HERMES_STATELESS_SESSION_PATTERNS",
        lcm_name="LCM_STATELESS_SESSION_PATTERNS",
    )
    ignore_patterns, ignore_source, ignore_overrides = _resolve_pattern_array(
        env_value=ignore_env,
        plugin_value=_pc_get(pc, "ignoreSessionPatterns", "ignore_session_patterns"),
    )
    stateless_patterns, stateless_source, stateless_overrides = _resolve_pattern_array(
        env_value=stateless_env,
        plugin_value=_pc_get(pc, "statelessSessionPatterns", "stateless_session_patterns"),
    )

    # Proactive threshold compaction mode.
    proactive_mode = (
        _to_proactive_mode(
            _read_env(
                env,
                hermes_name="HERMES_PROACTIVE_THRESHOLD_COMPACTION_MODE",
                lcm_name="LCM_PROACTIVE_THRESHOLD_COMPACTION_MODE",
            )
        )
        or _to_proactive_mode(
            _pc_get(pc, "proactiveThresholdCompactionMode", "proactive_threshold_compaction_mode")
        )
        or "deferred"
    )

    # Delegation timeout — TS uses toNumber (NOT parseFiniteInt) here, so
    # non-finite strings yield undefined and fall through to plugin/default.
    delegation_timeout_env_raw = _read_env(
        env,
        hermes_name="HERMES_DELEGATION_TIMEOUT_MS",
        lcm_name="LCM_DELEGATION_TIMEOUT_MS",
    )
    delegation_timeout_env = (
        _to_number(delegation_timeout_env_raw) if delegation_timeout_env_raw is not None else None
    )

    # Database path — TS reads env without trimming for LCM_DATABASE_PATH.
    db_env_raw = _read_env(
        env,
        hermes_name="HERMES_DATABASE_PATH",
        lcm_name="LCM_DATABASE_PATH",
    )

    # Large files dir — TS reads env WITH trimming.
    large_files_env_raw = _read_env(
        env,
        hermes_name="HERMES_LARGE_FILES_DIR",
        lcm_name="LCM_LARGE_FILES_DIR",
    )
    large_files_env_trimmed = (
        large_files_env_raw.strip() if large_files_env_raw is not None else None
    )

    state_dir = resolve_openclaw_state_dir(env)

    # Enabled — TS: env_LCM_ENABLED defined ⇒ value !== "false" (treats
    # any non-"false" string as true, even "" — see line 449-452 of TS).
    enabled_env = _read_env(
        env,
        hermes_name="HERMES_ENABLED",
        lcm_name="LCM_ENABLED",
    )
    enabled_pc = _to_bool(pc.get("enabled"))
    if enabled_env is not None:
        resolved_enabled = enabled_env != "false"
    elif enabled_pc is not None:
        resolved_enabled = enabled_pc
    else:
        resolved_enabled = True

    # Skip stateless sessions — TS: env "true" ⇒ True, else False (strict);
    # falls through to plugin/default when env is unset.
    skip_env = _read_env(
        env,
        hermes_name="HERMES_SKIP_STATELESS_SESSIONS",
        lcm_name="LCM_SKIP_STATELESS_SESSIONS",
    )
    skip_pc = _to_bool(_pc_get(pc, "skipStatelessSessions", "skip_stateless_sessions"))
    if skip_env is not None:
        resolved_skip = skip_env == "true"
    elif skip_pc is not None:
        resolved_skip = skip_pc
    else:
        resolved_skip = True

    # Prompt-aware eviction — strict "true" semantics.
    paw_env = _read_env(
        env,
        hermes_name="HERMES_PROMPT_AWARE_EVICTION_ENABLED",
        lcm_name="LCM_PROMPT_AWARE_EVICTION_ENABLED",
    )
    paw_pc = _to_bool(_pc_get(pc, "promptAwareEviction", "prompt_aware_eviction"))
    if paw_env is not None:
        resolved_paw = paw_env == "true"
    elif paw_pc is not None:
        resolved_paw = paw_pc
    else:
        resolved_paw = False

    # Prune heartbeat OK.
    pho_env = _read_env(
        env,
        hermes_name="HERMES_PRUNE_HEARTBEAT_OK",
        lcm_name="LCM_PRUNE_HEARTBEAT_OK",
    )
    pho_pc = _to_bool(_pc_get(pc, "pruneHeartbeatOk", "prune_heartbeat_ok"))
    if pho_env is not None:
        resolved_pho = pho_env == "true"
    elif pho_pc is not None:
        resolved_pho = pho_pc
    else:
        resolved_pho = False

    # Transcript GC enabled.
    tge_env = _read_env(
        env,
        hermes_name="HERMES_TRANSCRIPT_GC_ENABLED",
        lcm_name="LCM_TRANSCRIPT_GC_ENABLED",
    )
    tge_pc = _to_bool(_pc_get(pc, "transcriptGcEnabled", "transcript_gc_enabled"))
    if tge_env is not None:
        resolved_tge = tge_env == "true"
    elif tge_pc is not None:
        resolved_tge = tge_pc
    else:
        resolved_tge = False

    # Agent compaction tool enabled.
    act_env = _read_env(
        env,
        hermes_name="HERMES_AGENT_COMPACTION_TOOL_ENABLED",
        lcm_name="LCM_AGENT_COMPACTION_TOOL_ENABLED",
    )
    act_pc = _to_bool(
        _pc_get(pc, "agentCompactionToolEnabled", "agent_compaction_tool_enabled"),
    )
    if act_env is not None:
        resolved_act = act_env == "true"
    elif act_pc is not None:
        resolved_act = act_pc
    else:
        resolved_act = False

    # Auto-rotate enabled — TS uses ``!== "false"`` (any non-"false" ⇒ true).
    ars_en_env = _read_env(
        env,
        hermes_name="HERMES_AUTO_ROTATE_SESSION_FILES_ENABLED",
        lcm_name="LCM_AUTO_ROTATE_SESSION_FILES_ENABLED",
    )
    ars_en_pc = _to_bool((auto_rotate or {}).get("enabled"))
    if ars_en_env is not None:
        resolved_ars_enabled = ars_en_env != "false"
    elif ars_en_pc is not None:
        resolved_ars_enabled = ars_en_pc
    else:
        resolved_ars_enabled = True

    # Cache-aware compaction enabled — TS uses ``!== "false"``.
    cac_en_env = _read_env(
        env,
        hermes_name="HERMES_CACHE_AWARE_COMPACTION_ENABLED",
        lcm_name="LCM_CACHE_AWARE_COMPACTION_ENABLED",
    )
    cac_en_pc = _to_bool((cache_aware or {}).get("enabled"))
    if cac_en_env is not None:
        resolved_cac_enabled = cac_en_env != "false"
    elif cac_en_pc is not None:
        resolved_cac_enabled = cac_en_pc
    else:
        resolved_cac_enabled = True

    # Dynamic leaf chunk tokens enabled — TS uses STRICT "true" semantics
    # (not the !== "false" variant). See config.ts:602-604.
    dlct_en_env = _read_env(
        env,
        hermes_name="HERMES_DYNAMIC_LEAF_CHUNK_TOKENS_ENABLED",
        lcm_name="LCM_DYNAMIC_LEAF_CHUNK_TOKENS_ENABLED",
    )
    dlct_en_pc = _to_bool((dynamic_leaf or {}).get("enabled"))
    if dlct_en_env is not None:
        resolved_dlct_enabled = dlct_en_env == "true"
    elif dlct_en_pc is not None:
        resolved_dlct_enabled = dlct_en_pc
    else:
        resolved_dlct_enabled = True

    # Timezone — TS uses env.TZ (no LCM_ prefix; standard POSIX).
    timezone = env.get("TZ") or _to_str(pc.get("timezone")) or _system_timezone()

    # Fallback providers.
    fb_env = _parse_fallback_providers_env(
        _read_env(
            env,
            hermes_name="HERMES_FALLBACK_PROVIDERS",
            lcm_name="LCM_FALLBACK_PROVIDERS",
        )
    )
    fb_pc = _to_fallback_provider_array(
        _pc_get(pc, "fallbackProviders", "fallback_providers"),
    )
    fallback_providers = fb_env or fb_pc or []

    # --- Assemble the LcmConfig payload ---
    payload: dict[str, Any] = {
        "enabled": resolved_enabled,
        "database_path": (
            db_env_raw
            or _to_str(pc.get("dbPath"))
            or _to_str(pc.get("db_path"))
            or _to_str(pc.get("databasePath"))
            or _to_str(pc.get("database_path"))
            or str(Path(state_dir) / "lcm.db")
        ),
        "large_files_dir": (
            large_files_env_trimmed
            or _to_str(pc.get("largeFilesDir"))
            or _to_str(pc.get("large_files_dir"))
            or str(Path(state_dir) / "lcm-files")
        ),
        "ignore_session_patterns": ignore_patterns,
        "stateless_session_patterns": stateless_patterns,
        "skip_stateless_sessions": resolved_skip,
        "context_threshold": (
            _parse_finite_number(
                _read_env(
                    env,
                    hermes_name="HERMES_CONTEXT_THRESHOLD",
                    lcm_name="LCM_CONTEXT_THRESHOLD",
                )
            )
            or _to_number(pc.get("contextThreshold"))
            or _to_number(pc.get("context_threshold"))
            or 0.75
        ),
        "fresh_tail_count": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_FRESH_TAIL_COUNT",
                    lcm_name="LCM_FRESH_TAIL_COUNT",
                )
            )
            or _to_number(pc.get("freshTailCount"))
            or _to_number(pc.get("fresh_tail_count"))
            or 64
        ),
        "fresh_tail_max_tokens": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_FRESH_TAIL_MAX_TOKENS",
                    lcm_name="LCM_FRESH_TAIL_MAX_TOKENS",
                )
            )
            or _to_number(pc.get("freshTailMaxTokens"))
            or _to_number(pc.get("fresh_tail_max_tokens"))
        ),
        "prompt_aware_eviction": resolved_paw,
        "new_session_retain_depth": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_NEW_SESSION_RETAIN_DEPTH",
                    lcm_name="LCM_NEW_SESSION_RETAIN_DEPTH",
                )
            )
            or _to_number(pc.get("newSessionRetainDepth"))
            or _to_number(pc.get("new_session_retain_depth"))
            or 2
        ),
        "leaf_min_fanout": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_LEAF_MIN_FANOUT",
                    lcm_name="LCM_LEAF_MIN_FANOUT",
                )
            )
            or _to_number(pc.get("leafMinFanout"))
            or _to_number(pc.get("leaf_min_fanout"))
            or 8
        ),
        "condensed_min_fanout": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_CONDENSED_MIN_FANOUT",
                    lcm_name="LCM_CONDENSED_MIN_FANOUT",
                )
            )
            or _to_number(pc.get("condensedMinFanout"))
            or _to_number(pc.get("condensed_min_fanout"))
            or 4
        ),
        "condensed_min_fanout_hard": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_CONDENSED_MIN_FANOUT_HARD",
                    lcm_name="LCM_CONDENSED_MIN_FANOUT_HARD",
                )
            )
            or _to_number(pc.get("condensedMinFanoutHard"))
            or _to_number(pc.get("condensed_min_fanout_hard"))
            or 2
        ),
        "incremental_max_depth": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_INCREMENTAL_MAX_DEPTH",
                    lcm_name="LCM_INCREMENTAL_MAX_DEPTH",
                )
            )
            if _read_env(
                env,
                hermes_name="HERMES_INCREMENTAL_MAX_DEPTH",
                lcm_name="LCM_INCREMENTAL_MAX_DEPTH",
            )
            is not None
            else None
        )
        or _to_number(pc.get("incrementalMaxDepth"))
        or _to_number(pc.get("incremental_max_depth"))
        or 1,
        "leaf_chunk_tokens": resolved_leaf_chunk_tokens,
        "bootstrap_max_tokens": resolved_bootstrap_max_tokens,
        "leaf_target_tokens": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_LEAF_TARGET_TOKENS",
                    lcm_name="LCM_LEAF_TARGET_TOKENS",
                )
            )
            or _to_number(pc.get("leafTargetTokens"))
            or _to_number(pc.get("leaf_target_tokens"))
            or 4000
        ),
        "condensed_target_tokens": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_CONDENSED_TARGET_TOKENS",
                    lcm_name="LCM_CONDENSED_TARGET_TOKENS",
                )
            )
            or _to_number(pc.get("condensedTargetTokens"))
            or _to_number(pc.get("condensed_target_tokens"))
            or 2000
        ),
        "max_expand_tokens": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_MAX_EXPAND_TOKENS",
                    lcm_name="LCM_MAX_EXPAND_TOKENS",
                )
            )
            or _to_number(pc.get("maxExpandTokens"))
            or _to_number(pc.get("max_expand_tokens"))
            or 4000
        ),
        "large_file_token_threshold": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_LARGE_FILE_TOKEN_THRESHOLD",
                    lcm_name="LCM_LARGE_FILE_TOKEN_THRESHOLD",
                )
            )
            or _to_number(pc.get("largeFileThresholdTokens"))
            or _to_number(pc.get("large_file_threshold_tokens"))
            or _to_number(pc.get("largeFileTokenThreshold"))
            or _to_number(pc.get("large_file_token_threshold"))
            or 25000
        ),
        "summary_provider": (
            (
                _read_env(
                    env, hermes_name="HERMES_SUMMARY_PROVIDER", lcm_name="LCM_SUMMARY_PROVIDER"
                )
                or ""
            ).strip()
            or _to_str(pc.get("summaryProvider"))
            or _to_str(pc.get("summary_provider"))
            or ""
        ),
        "summary_model": (
            (
                _read_env(env, hermes_name="HERMES_SUMMARY_MODEL", lcm_name="LCM_SUMMARY_MODEL")
                or ""
            ).strip()
            or _to_str(pc.get("summaryModel"))
            or _to_str(pc.get("summary_model"))
            or ""
        ),
        "large_file_summary_provider": (
            (
                _read_env(
                    env,
                    hermes_name="HERMES_LARGE_FILE_SUMMARY_PROVIDER",
                    lcm_name="LCM_LARGE_FILE_SUMMARY_PROVIDER",
                )
                or ""
            ).strip()
            or _to_str(pc.get("largeFileSummaryProvider"))
            or _to_str(pc.get("large_file_summary_provider"))
            or ""
        ),
        "large_file_summary_model": (
            (
                _read_env(
                    env,
                    hermes_name="HERMES_LARGE_FILE_SUMMARY_MODEL",
                    lcm_name="LCM_LARGE_FILE_SUMMARY_MODEL",
                )
                or ""
            ).strip()
            or _to_str(pc.get("largeFileSummaryModel"))
            or _to_str(pc.get("large_file_summary_model"))
            or ""
        ),
        "expansion_provider": (
            (
                _read_env(
                    env,
                    hermes_name="HERMES_EXPANSION_PROVIDER",
                    lcm_name="LCM_EXPANSION_PROVIDER",
                )
                or ""
            ).strip()
            or _to_str(pc.get("expansionProvider"))
            or _to_str(pc.get("expansion_provider"))
            or ""
        ),
        "expansion_model": (
            (
                _read_env(
                    env,
                    hermes_name="HERMES_EXPANSION_MODEL",
                    lcm_name="LCM_EXPANSION_MODEL",
                )
                or ""
            ).strip()
            or _to_str(pc.get("expansionModel"))
            or _to_str(pc.get("expansion_model"))
            or ""
        ),
        "delegation_timeout_ms": (
            delegation_timeout_env
            or _to_number(pc.get("delegationTimeoutMs"))
            or _to_number(pc.get("delegation_timeout_ms"))
            or 120000
        ),
        "summary_timeout_ms": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_SUMMARY_TIMEOUT_MS",
                    lcm_name="LCM_SUMMARY_TIMEOUT_MS",
                )
            )
            or _to_number(pc.get("summaryTimeoutMs"))
            or _to_number(pc.get("summary_timeout_ms"))
            or 60000
        ),
        "timezone": timezone,
        "prune_heartbeat_ok": resolved_pho,
        "transcript_gc_enabled": resolved_tge,
        "agent_compaction_tool_enabled": resolved_act,
        "proactive_threshold_compaction_mode": proactive_mode,
        "auto_rotate_session_files": {
            "enabled": resolved_ars_enabled,
            "size_bytes": auto_rotate_size,
            "startup": (
                _to_auto_rotate_mode(
                    _read_env(
                        env,
                        hermes_name="HERMES_AUTO_ROTATE_SESSION_FILES_STARTUP",
                        lcm_name="LCM_AUTO_ROTATE_SESSION_FILES_STARTUP",
                    )
                )
                or _to_auto_rotate_mode((auto_rotate or {}).get("startup"))
                or "rotate"
            ),
            "runtime": (
                _to_auto_rotate_mode(
                    _read_env(
                        env,
                        hermes_name="HERMES_AUTO_ROTATE_SESSION_FILES_RUNTIME",
                        lcm_name="LCM_AUTO_ROTATE_SESSION_FILES_RUNTIME",
                    )
                )
                or _to_auto_rotate_mode((auto_rotate or {}).get("runtime"))
                or "rotate"
            ),
        },
        "max_assembly_token_budget": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_MAX_ASSEMBLY_TOKEN_BUDGET",
                    lcm_name="LCM_MAX_ASSEMBLY_TOKEN_BUDGET",
                )
            )
            or _to_number(pc.get("maxAssemblyTokenBudget"))
            or _to_number(pc.get("max_assembly_token_budget"))
        ),
        "tool_result_token_budget": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_TOOL_RESULT_TOKEN_BUDGET",
                    lcm_name="LCM_TOOL_RESULT_TOKEN_BUDGET",
                )
            )
            or _to_number(pc.get("toolResultTokenBudget"))
            or _to_number(pc.get("tool_result_token_budget"))
        ),
        "summary_max_overage_factor": (
            _parse_finite_number(
                _read_env(
                    env,
                    hermes_name="HERMES_SUMMARY_MAX_OVERAGE_FACTOR",
                    lcm_name="LCM_SUMMARY_MAX_OVERAGE_FACTOR",
                )
            )
            or _to_number(pc.get("summaryMaxOverageFactor"))
            or _to_number(pc.get("summary_max_overage_factor"))
            or 3.0
        ),
        "custom_instructions": (
            (
                _read_env(
                    env,
                    hermes_name="HERMES_CUSTOM_INSTRUCTIONS",
                    lcm_name="LCM_CUSTOM_INSTRUCTIONS",
                )
                or ""
            ).strip()
            or _to_str(pc.get("customInstructions"))
            or _to_str(pc.get("custom_instructions"))
            or ""
        ),
        "circuit_breaker_threshold": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_CIRCUIT_BREAKER_THRESHOLD",
                    lcm_name="LCM_CIRCUIT_BREAKER_THRESHOLD",
                )
            )
            or _to_number(pc.get("circuitBreakerThreshold"))
            or _to_number(pc.get("circuit_breaker_threshold"))
            or 5
        ),
        "circuit_breaker_cooldown_ms": (
            _parse_finite_int(
                _read_env(
                    env,
                    hermes_name="HERMES_CIRCUIT_BREAKER_COOLDOWN_MS",
                    lcm_name="LCM_CIRCUIT_BREAKER_COOLDOWN_MS",
                )
            )
            or _to_number(pc.get("circuitBreakerCooldownMs"))
            or _to_number(pc.get("circuit_breaker_cooldown_ms"))
            or 1_800_000
        ),
        "fallback_providers": fallback_providers,
        "cache_aware_compaction": {
            "enabled": resolved_cac_enabled,
            "cache_ttl_seconds": (
                _parse_finite_int(
                    _read_env(
                        env,
                        hermes_name="HERMES_CACHE_TTL_SECONDS",
                        lcm_name="LCM_CACHE_TTL_SECONDS",
                    )
                )
                or _to_number(_pc_get(cache_aware or {}, "cacheTTLSeconds", "cache_ttl_seconds"))
                or 300
            ),
            "max_cold_cache_catchup_passes": (
                _parse_finite_int(
                    _read_env(
                        env,
                        hermes_name="HERMES_MAX_COLD_CACHE_CATCHUP_PASSES",
                        lcm_name="LCM_MAX_COLD_CACHE_CATCHUP_PASSES",
                    )
                )
                or _to_number(
                    _pc_get(
                        cache_aware or {},
                        "maxColdCacheCatchupPasses",
                        "max_cold_cache_catchup_passes",
                    )
                )
                or 2
            ),
            "hot_cache_pressure_factor": resolved_hot_pressure,
            "hot_cache_budget_headroom_ratio": resolved_hot_headroom,
            "cold_cache_observation_threshold": resolved_cold_thresh,
            "critical_budget_pressure_ratio": resolved_crit,
        },
        "dynamic_leaf_chunk_tokens": {
            "enabled": resolved_dlct_enabled,
            "max": resolved_dynamic_max,
        },
        "voyage_api_key": _to_str(pc.get("voyageApiKey")) or _to_str(pc.get("voyage_api_key")),
    }

    # Coerce floats that should be ints for the typed model.
    payload["fresh_tail_count"] = int(payload["fresh_tail_count"])
    payload["new_session_retain_depth"] = int(payload["new_session_retain_depth"])
    payload["leaf_min_fanout"] = int(payload["leaf_min_fanout"])
    payload["condensed_min_fanout"] = int(payload["condensed_min_fanout"])
    payload["condensed_min_fanout_hard"] = int(payload["condensed_min_fanout_hard"])
    payload["incremental_max_depth"] = int(payload["incremental_max_depth"])
    payload["leaf_target_tokens"] = int(payload["leaf_target_tokens"])
    payload["condensed_target_tokens"] = int(payload["condensed_target_tokens"])
    payload["max_expand_tokens"] = int(payload["max_expand_tokens"])
    payload["large_file_token_threshold"] = int(payload["large_file_token_threshold"])
    payload["delegation_timeout_ms"] = int(payload["delegation_timeout_ms"])
    payload["summary_timeout_ms"] = int(payload["summary_timeout_ms"])
    payload["circuit_breaker_threshold"] = int(payload["circuit_breaker_threshold"])
    payload["circuit_breaker_cooldown_ms"] = int(payload["circuit_breaker_cooldown_ms"])
    if payload["fresh_tail_max_tokens"] is not None:
        payload["fresh_tail_max_tokens"] = int(payload["fresh_tail_max_tokens"])
    if payload["max_assembly_token_budget"] is not None:
        payload["max_assembly_token_budget"] = int(payload["max_assembly_token_budget"])
    if payload["tool_result_token_budget"] is not None:
        payload["tool_result_token_budget"] = int(payload["tool_result_token_budget"])

    config = LcmConfig(**payload)
    diagnostics = LcmConfigDiagnostics(
        ignore_session_patterns_source=ignore_source,
        stateless_session_patterns_source=stateless_source,
        ignore_session_patterns_env_overrides_config=ignore_overrides,
        stateless_session_patterns_env_overrides_config=stateless_overrides,
    )
    return config, diagnostics


def resolve_lcm_config(
    env: Mapping[str, str] | None = None,
    plugin_config: Mapping[str, Any] | None = None,
) -> LcmConfig:
    """Resolve ``LcmConfig`` from env + plugin-config (no diagnostics).

    Mirrors TS ``resolveLcmConfig`` (config.ts:624-629). Convenience
    wrapper around :func:`resolve_lcm_config_with_diagnostics` for
    callers that don't need pattern-source provenance.
    """
    return resolve_lcm_config_with_diagnostics(env, plugin_config)[0]


def _system_timezone() -> str:
    """Best-effort system timezone — matches TS
    ``Intl.DateTimeFormat().resolvedOptions().timeZone``.

    Falls back to ``"UTC"`` if the platform doesn't expose the IANA
    name (rare on macOS/Linux; possible on minimal containers).
    """
    try:
        # Python 3.9+ stdlib path.
        import zoneinfo

        # Try /etc/localtime first (cross-platform on Linux/macOS).
        from datetime import datetime

        local = datetime.now().astimezone()
        if local.tzinfo is not None:
            tz_name = str(local.tzinfo)
            if tz_name and tz_name not in ("UTC", "local"):
                # Round-trip through zoneinfo to validate it's a known IANA name.
                try:
                    zoneinfo.ZoneInfo(tz_name)
                    return tz_name
                except Exception:
                    pass
        # Fallback to /etc/localtime probe.
        local_path = Path("/etc/localtime")
        if local_path.is_symlink():
            target = os.readlink(local_path)
            # /etc/localtime → /usr/share/zoneinfo/America/Los_Angeles
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    return "UTC"


# ---------------------------------------------------------------------------
# YAML loader (Hermes-side config delivery per ADR-023)
# ---------------------------------------------------------------------------

# Mirror of ``hermes_cli.config._expand_env_vars``'s regex (per the v0
# skeleton's docstring). Kept local so the lossless-hermes test suite
# can run without hermes-agent on the import path (per ADR-007).
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ``${VAR}`` references in config values.

    Only string scalars are processed. Unresolved references (variable
    not in :data:`os.environ`) are kept verbatim. Matches Hermes
    ``_expand_env_vars`` behavior 1:1 — see v0 skeleton docstring.
    """
    if isinstance(obj, str):
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)),
            obj,
        )
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(item) for item in obj]
    return obj


def load_config(path: Path | None = None) -> LcmConfig:
    """Load and validate the lossless-hermes operator config.

    Reads YAML from ``path`` (default ``$HERMES_HOME/config.yaml``),
    extracts the top-level ``lossless_hermes:`` namespace per ADR-023,
    interpolates ``${VAR}`` references against :data:`os.environ`, and
    feeds the subtree through :func:`resolve_lcm_config` to apply env-
    var overrides + defaults.

    This is the v0 → v1 contract preserved verbatim: existing callers
    of ``load_config(path)`` continue to work; the model is now fully
    populated instead of empty.

    Behavior:

    * Missing file ⇒ ``resolve_lcm_config(env=os.environ, plugin_config={})``
      (env-driven defaults). NOT ``LcmConfig()`` directly — that would
      bypass env vars like ``HERMES_DATABASE_PATH``.
    * Off-shape YAML root (bare string / list) ⇒ defaults (same path).
    * Off-shape ``lossless_hermes:`` value (string/list/number under
      the namespace) ⇒ ``TypeError`` via pydantic — louder than
      silent defaults.
    * Unknown key under ``lossless_hermes:`` ⇒
      :class:`pydantic.ValidationError` because ``LcmConfig`` is
      declared with ``extra='forbid'`` (ADR-023 §Consequences).

    Args:
        path: Optional override for the YAML file location. ``None``
            ⇒ ``$HERMES_HOME/config.yaml``.

    Returns:
        A validated :class:`LcmConfig` instance with env + YAML +
        defaults applied per the TS precedence rules.

    Raises:
        pydantic.ValidationError: Unknown key under ``lossless_hermes:``.
        yaml.YAMLError: File exists but is not valid YAML.
    """
    if path is None:
        path = Path(resolve_hermes_state_dir()) / "config.yaml"

    if not path.exists():
        return resolve_lcm_config(env=os.environ, plugin_config={})

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        return resolve_lcm_config(env=os.environ, plugin_config={})

    expanded = _expand_env_vars(raw)
    subtree = expanded.get("lossless_hermes", {}) or {}

    if not isinstance(subtree, dict):
        # Forward off-shape to the resolver — pydantic raises.
        return LcmConfig(**subtree)

    # Validate that every plugin-config key under ``lossless_hermes:`` is a
    # known LcmConfig field (or one of its legacy TS aliases). Mirrors the
    # ``additionalProperties: false`` contract from
    # ``openclaw.plugin.json``'s configSchema. Catches typos at startup
    # rather than letting them silently no-op through the resolver.
    _validate_subtree_keys(subtree)

    return resolve_lcm_config(env=os.environ, plugin_config=subtree)


# Set of recognized plugin-config keys (both Python snake_case and TS
# camelCase aliases — the resolver accepts both per the TS contract that
# JSON-from-disk and pydantic-natural names round-trip).
_RECOGNIZED_PLUGIN_CONFIG_KEYS: frozenset[str] = frozenset({
    # Python snake_case (canonical)
    "enabled",
    "database_path",
    "db_path",  # alias for database_path
    "large_files_dir",
    "ignore_session_patterns",
    "stateless_session_patterns",
    "skip_stateless_sessions",
    "context_threshold",
    "fresh_tail_count",
    "fresh_tail_max_tokens",
    "prompt_aware_eviction",
    "new_session_retain_depth",
    "leaf_min_fanout",
    "condensed_min_fanout",
    "condensed_min_fanout_hard",
    "incremental_max_depth",
    "leaf_chunk_tokens",
    "bootstrap_max_tokens",
    "leaf_target_tokens",
    "condensed_target_tokens",
    "max_expand_tokens",
    "large_file_token_threshold",
    "large_file_threshold_tokens",  # alias
    "summary_provider",
    "summary_model",
    "large_file_summary_provider",
    "large_file_summary_model",
    "expansion_provider",
    "expansion_model",
    "delegation_timeout_ms",
    "summary_timeout_ms",
    "timezone",
    "prune_heartbeat_ok",
    "transcript_gc_enabled",
    "agent_compaction_tool_enabled",
    "proactive_threshold_compaction_mode",
    "auto_rotate_session_files",
    "max_assembly_token_budget",
    "tool_result_token_budget",
    "summary_max_overage_factor",
    "custom_instructions",
    "circuit_breaker_threshold",
    "circuit_breaker_cooldown_ms",
    "fallback_providers",
    "cache_aware_compaction",
    "dynamic_leaf_chunk_tokens",
    "voyage_api_key",
    "workers",
    # TS camelCase aliases (accepted for JSON-from-disk round-tripping)
    "databasePath",
    "dbPath",
    "largeFilesDir",
    "ignoreSessionPatterns",
    "statelessSessionPatterns",
    "skipStatelessSessions",
    "contextThreshold",
    "freshTailCount",
    "freshTailMaxTokens",
    "promptAwareEviction",
    "newSessionRetainDepth",
    "leafMinFanout",
    "condensedMinFanout",
    "condensedMinFanoutHard",
    "incrementalMaxDepth",
    "leafChunkTokens",
    "bootstrapMaxTokens",
    "leafTargetTokens",
    "condensedTargetTokens",
    "maxExpandTokens",
    "largeFileTokenThreshold",
    "largeFileThresholdTokens",
    "summaryProvider",
    "summaryModel",
    "largeFileSummaryProvider",
    "largeFileSummaryModel",
    "expansionProvider",
    "expansionModel",
    "delegationTimeoutMs",
    "summaryTimeoutMs",
    "pruneHeartbeatOk",
    "transcriptGcEnabled",
    "agentCompactionToolEnabled",
    "proactiveThresholdCompactionMode",
    "autoRotateSessionFiles",
    "maxAssemblyTokenBudget",
    "toolResultTokenBudget",
    "summaryMaxOverageFactor",
    "customInstructions",
    "circuitBreakerThreshold",
    "circuitBreakerCooldownMs",
    "fallbackProviders",
    "cacheAwareCompaction",
    "dynamicLeafChunkTokens",
    "voyageApiKey",
})


def _validate_subtree_keys(subtree: Mapping[str, Any]) -> None:
    """Reject unknown keys under ``lossless_hermes:``.

    Mirrors ``additionalProperties: false`` from
    ``openclaw.plugin.json``'s configSchema (line 502). Catches typos
    at startup rather than silently dropping them through the
    resolver. Raises :class:`pydantic.ValidationError` with the
    unknown key name so the operator can fix the YAML.
    """
    unknown = sorted(set(subtree.keys()) - _RECOGNIZED_PLUGIN_CONFIG_KEYS)
    if unknown:
        # Pydantic-style structured error so existing test code
        # (`exc.value.errors()[0]["input"]`) keeps working.
        from pydantic_core import InitErrorDetails, PydanticCustomError

        err_details: list[InitErrorDetails] = [
            {
                "type": PydanticCustomError(
                    "extra_forbidden",
                    "Extra inputs are not permitted",
                ),
                "loc": (key,),
                "input": subtree[key],
            }
            for key in unknown
        ]
        from pydantic import ValidationError

        raise ValidationError.from_exception_data(
            title="LcmConfig",
            line_errors=err_details,
        )


# ---------------------------------------------------------------------------
# Voyage credential resolver (per ADR-022)
# ---------------------------------------------------------------------------


def resolve_voyage_api_key(
    config: LcmConfig,
    *,
    env: Mapping[str, str] | None = None,
    hermes_home: Path | None = None,
) -> str | None:
    """Resolve the Voyage API key per ADR-022 three-tier precedence.

    Tiers (strict, first non-empty wins after stripping whitespace):

    1. ``config.voyage_api_key`` — inline in ``~/.hermes/config.yaml``
       (supports ``${VOYAGE_API_KEY}`` interpolation at YAML load time).
    2. ``env["VOYAGE_API_KEY"]`` — standard CI / 12-factor mechanism.
    3. ``$HERMES_HOME/lossless-hermes/credentials/voyage-api-key`` —
       file contents, mirrors the OpenClaw ``~/.openclaw/credentials/
       voyage-api-key`` layout for migration friction.

    Returns ``None`` if no tier yields a non-empty value (callers that
    require a key — i.e. live Voyage clients — should raise their
    own ``VoyageAuthError`` with the actionable message; this function
    is purely a lookup helper to keep the embeddings module decoupled
    from filesystem layout).

    Args:
        config: The resolved ``LcmConfig`` (tier-1 source).
        env: Optional env mapping (defaults to :data:`os.environ`).
        hermes_home: Optional ``HERMES_HOME`` override (defaults to
            ``resolve_hermes_state_dir(env)``).

    Returns:
        The first non-empty trimmed value, or ``None`` if all tiers
        are empty.
    """
    if env is None:
        env = os.environ
    if hermes_home is None:
        hermes_home = Path(resolve_hermes_state_dir(env))

    # Tier 1: config inline.
    if config.voyage_api_key:
        stripped = config.voyage_api_key.strip()
        if stripped:
            return stripped

    # Tier 2: env var.
    env_val = env.get("VOYAGE_API_KEY", "").strip()
    if env_val:
        return env_val

    # Tier 3: $HERMES_HOME/lossless-hermes/credentials/voyage-api-key
    cred_path = hermes_home / "lossless-hermes" / "credentials" / "voyage-api-key"
    if cred_path.exists():
        try:
            file_val = cred_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if file_val:
            return file_val

    return None
