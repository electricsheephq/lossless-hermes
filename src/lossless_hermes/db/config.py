"""Operator config skeleton for lossless-hermes.

Defines a Pydantic v2 model (``LcmConfig``) that represents the v0
operator-config surface — currently **empty** — plus a loader
(:func:`load_config`) that reads ``~/.hermes/config.yaml``, extracts the
top-level ``lossless_hermes:`` namespace per ADR-023 §Decision, performs
``${VAR}`` env-var interpolation, and returns a validated ``LcmConfig``
instance.

### v0 surface

This module is intentionally a **skeleton** (per ADR-023 §Consequences and
issue #00-07 §Acceptance criteria):

* ``LcmConfig`` has **no fields** at v0. ``model_config = ConfigDict(extra='forbid')``
  ensures any user-supplied key under ``lossless_hermes:`` raises a typed
  :class:`pydantic.ValidationError` at startup — typos surface immediately
  instead of silently falling through to a missing-feature bug later.
* ``WorkerConfig`` is a placeholder nested model for the workers map
  (per-worker intervals etc.) that lands in Epic 02 — see ADR-020
  (worker-loop dispatcher).

Every real configuration knob lands in a **separate PR** that ports the
corresponding TS source (one PR per subsystem). The Pydantic shape for env-
var fallbacks is documented in the example comment block at the bottom of
this module; it's not used until a knob actually lands.

### Namespace resolution

ADR-023 §Decision specifies **top-level** ``lossless_hermes:`` in
``~/.hermes/config.yaml`` (snake_case keys). The earlier task wording
("`context.lossless_hermes.*`") is reconciled here in favor of the ADR.
See issue #00-07 §"Acceptance criteria" line 32 and the worked example in
``docs/reference/hermes-hooks.md``. Note: hermes-hooks.md shows an
alternative pattern (``plugins.entries.lossless-hermes:`` under
``plugins:``) — ADR-023 supersedes that for plugin-config delivery.

Example:

.. code-block:: yaml

    # ~/.hermes/config.yaml
    context:
      engine: lcm           # top-level selector (Hermes-host concern)
    plugins:
      enabled:
        - lossless-hermes
    lossless_hermes:        # ← plugin-owned namespace (this module's scope)
      # (empty for v0 — every knob lands in a later issue)

### ``${VAR}`` interpolation

When the YAML body contains a ``${SOME_VAR}`` string, the loader replaces
it with ``os.environ["SOME_VAR"]`` at parse time. Unresolved references
(variable absent from the environment) are left **verbatim** so callers
can detect them. This matches Hermes's behavior 1:1 (see
``hermes_cli/config.py:_expand_env_vars`` — the regex
``\\${([^}]+)}`` is shared) so an operator using the same config.yaml
with and without lossless-hermes installed sees identical expansion.

We re-implement the regex locally instead of importing from Hermes for
two reasons: (1) the lossless-hermes test suite runs without hermes-agent
installed (ADR-007 — host-installed, not pinned) so the bridge raises at
import; (2) the regex is a well-defined, stable contract that costs ~5
LOC to mirror.

See:

* ADR-023 §Decision and §Consequences — full loader spec, including the
  ``Field(default=..., validation_alias=AliasChoices(...))`` shape for
  env-var fallbacks when knobs actually land.
* ADR-022 — Voyage credential resolution. The ``voyage_api_key`` field
  is tier-1 of the three-tier resolver. (Resolver and field land in
  the embeddings epic; not in scope for #00-07.)
* ADR-024 §Decision — ``db/config.py`` placement under the 1:1 mirror.
* ``docs/porting-guides/tests-and-config.md`` §"Configuration surface
  — full inventory" — the full TS field list (67 env vars, ~50 typed
  knobs) that lands incrementally in subsequent issues.

### Field-addition policy

Each new field is its own PR that ports the corresponding TS knob. The
review template asks the author to cite the TS source line, the default
value, the env-var alias (if any), and the test that validates the value.
The skeleton stays empty until a knob is needed by a landing subsystem.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

__all__ = ["LcmConfig", "WorkerConfig", "load_config"]


# ---------------------------------------------------------------------------
# ``${VAR}`` interpolation
# ---------------------------------------------------------------------------

# Mirror of ``hermes_cli.config._expand_env_vars``'s regex (see module
# docstring). Kept local so the lossless-hermes test suite can run without
# hermes-agent on the import path (per ADR-007 §Decision "Hermes is host-
# installed, not pinned"). The regex matches the TS template syntax used
# by Hermes's existing config.yaml workflow.
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ``${VAR}`` references in config values.

    Only string scalars are processed; dict keys, numbers, booleans, lists-
    of-non-strings, and ``None`` pass through untouched. Unresolved
    references (variable not in :data:`os.environ`) are kept verbatim so a
    caller can detect them downstream (matches Hermes behavior 1:1 —
    see ``hermes_cli.config._expand_env_vars``).
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


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class WorkerConfig(BaseModel):
    """Per-worker configuration (placeholder).

    Empty for v0. The workers map (``workers.embedding_backfill``,
    ``workers.entity_extraction``, ``workers.condensation_maintenance``,
    etc.) lands when the worker-loop dispatcher ports in Epic 02 — see
    ADR-020 §"Worker loop dispatcher". Fields like ``interval_s``,
    ``enabled``, and per-worker overrides will arrive on individual PRs
    as each worker subsystem moves from stub to live.

    ``extra='forbid'`` is set so an operator who mistypes ``intrval_s``
    (or any other future field) gets a typed validation error at startup
    rather than a silently dropped knob.
    """

    model_config = ConfigDict(extra="forbid")


class LcmConfig(BaseModel):
    """Lossless context-management operator config (skeleton).

    **Empty for v0** by design. Every operator knob (compaction
    thresholds, Voyage models, summary providers, worker intervals,
    fallback chains, debug flags, etc.) ports in subsequent issues as
    the relevant subsystem moves from stub to live. The full inventory
    of ~50 typed fields and ~67 env vars is catalogued in
    ``docs/porting-guides/tests-and-config.md`` §"Configuration surface
    — full inventory". Each field arrives on its own PR with cited TS
    source, default, env alias, and a unit test.

    ``model_config = ConfigDict(extra='forbid')`` is load-bearing — it
    means an operator who writes ``lossless_hermes: {threshhold: 0.7}``
    (typo) gets a clear startup error instead of silently running with
    the default. ADR-023 §Consequences pins this contract.

    When a TS source uses an env-var fallback (e.g. ``LCM_SUMMARY_MODEL``
    or ``LCM_CONTEXT_THRESHOLD``), the porting PR uses Pydantic's
    ``validation_alias=AliasChoices(...)`` shape so the same env name
    continues to work. Example (illustrative — not active until a real
    knob lands)::

        from pydantic import AliasChoices, Field

        class LcmConfig(BaseModel):
            model_config = ConfigDict(extra="forbid")

            summary_model: str = Field(
                default="",
                validation_alias=AliasChoices("summary_model", "LCM_SUMMARY_MODEL"),
            )
            context_threshold: float = Field(
                default=0.75,
                ge=0.0,
                le=1.0,
                validation_alias=AliasChoices("context_threshold", "LCM_CONTEXT_THRESHOLD"),
            )
            workers: dict[str, WorkerConfig] = Field(default_factory=dict)

    The skeleton stays empty until a subsystem requires the knob — keeps
    the back-port story simple (every TS field has a known one-PR target).
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    """Resolve ``HERMES_HOME`` without forcing a Hermes import.

    Mirrors ``hermes_constants.get_hermes_home``'s default-resolution path
    (env var → ``~/.hermes``). We re-implement here so this module is
    importable in a Hermes-less test environment (ADR-007 §Decision
    "Hermes is host-installed, not pinned"). When Hermes is installed,
    both resolutions point at the same directory.
    """
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    return Path.home() / ".hermes"


def load_config(path: Path | None = None) -> LcmConfig:
    """Load and validate the lossless-hermes operator config.

    Reads YAML from ``path`` (default ``$HERMES_HOME/config.yaml``,
    falling back to ``~/.hermes/config.yaml`` if ``HERMES_HOME`` is
    unset). The top-level ``lossless_hermes:`` key is treated as the
    plugin's namespace per ADR-023 §Decision. Unknown keys under that
    namespace raise :class:`pydantic.ValidationError` because
    ``LcmConfig`` is declared with ``extra='forbid'``.

    Behavior:

    * If ``path`` is ``None``, defaults to
      ``$HERMES_HOME/config.yaml`` (or ``~/.hermes/config.yaml`` when
      the env var is unset). This matches Hermes's canonical config
      location per ADR-023 §Context.
    * If the file does not exist on disk, returns ``LcmConfig()`` (the
      default-everything model). This is the typical first-run path —
      operators don't need to seed a config file just to install the
      plugin.
    * If the file exists, it is parsed via :func:`yaml.safe_load`.
    * ``${VAR}`` references in the YAML body are expanded against
      :data:`os.environ` before model construction (see
      :func:`_expand_env_vars` for the regex contract).
    * The ``lossless_hermes:`` subtree is then passed to
      ``LcmConfig(**subtree)``. Missing namespace ⇒ ``LcmConfig()``;
      empty mapping ⇒ ``LcmConfig()``; any unknown field ⇒ a typed
      ``pydantic.ValidationError``.

    Args:
        path: Optional override for the YAML file location. ``None`` ⇒
            ``$HERMES_HOME/config.yaml``.

    Returns:
        A validated :class:`LcmConfig` instance.

    Raises:
        pydantic.ValidationError: An unknown key was found under the
            ``lossless_hermes:`` namespace (typo or removed knob).
        yaml.YAMLError: The file exists but is not valid YAML.
    """
    if path is None:
        path = _resolve_hermes_home() / "config.yaml"

    if not path.exists():
        return LcmConfig()

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # An off-shape top-level (e.g. file is a bare string or list, not a
    # mapping) falls through to defaults. Mirrors Hermes's
    # ``read_raw_config`` precedent in ``hermes_cli.config`` — tolerating
    # malformed-but-non-empty YAML at the root and surfacing the actual
    # error path (missing/unknown keys) through Pydantic instead.
    if not isinstance(raw, dict):
        return LcmConfig()

    expanded = _expand_env_vars(raw)
    subtree = expanded.get("lossless_hermes", {}) or {}

    # ``lossless_hermes:`` present but explicitly off-shape (string,
    # list, number). Forward to Pydantic — ``LcmConfig(**non_mapping)``
    # raises a ``TypeError`` which is louder than silently defaulting.
    # An empty mapping or the key being absent still yields ``LcmConfig()``.
    return LcmConfig(**subtree)
