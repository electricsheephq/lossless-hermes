"""Loader-shape tests for ``lossless_hermes.db.config.load_config``.

Originally written as a v0 skeleton (issue #00-07). Updated for issue
#01-02 — the model now has all ~52 fields per the TS source, so
"empty model" assertions have been replaced with "sensible defaults"
checks. The loader contract is otherwise unchanged:

1. Missing config file ⇒ defaults applied (loader does NOT raise).
2. ``lossless_hermes: {}`` is the explicit-empty case → defaults.
3. An unknown key under ``lossless_hermes:`` ⇒
   :class:`pydantic.ValidationError` because ``LcmConfig`` is declared
   with ``extra='forbid'`` (ADR-023 §Consequences).
4. ``${VAR}`` references in the YAML body are interpolated against
   :data:`os.environ` when the variable is set.

Field-level coverage (precedence, clamps, derived defaults, alias
deprecation warnings) lives in ``test_db_config.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lossless_hermes.db.config import LcmConfig, WorkerConfig, load_config


# ---------------------------------------------------------------------------
# Model surface — v0 skeleton invariants
# ---------------------------------------------------------------------------


def test_lcm_config_instantiates_with_no_args() -> None:
    """``LcmConfig()`` constructs with all defaults from the TS source.

    Post-01-02 (full model port), the constructor applies ~50 default
    values matching ``src/db/config.ts``. We assert on a handful of
    load-bearing ones so a future PR that drifts a default has a clear
    failure here. Full field coverage lives in ``test_db_config.py``.
    """
    cfg = LcmConfig()
    # Sample of the contract: every TS default mirrors here.
    assert cfg.enabled is True
    assert cfg.context_threshold == 0.75
    assert cfg.leaf_chunk_tokens == 20000
    assert cfg.leaf_target_tokens == 4000  # v4.1 (A.10): raised from 2400
    assert cfg.fresh_tail_count == 64
    assert cfg.proactive_threshold_compaction_mode == "deferred"
    assert cfg.summary_max_overage_factor == 3.0
    assert cfg.cache_aware_compaction.enabled is True
    assert cfg.cache_aware_compaction.cache_ttl_seconds == 300
    assert cfg.dynamic_leaf_chunk_tokens.enabled is True


def test_lcm_config_rejects_unknown_field_at_construct_time() -> None:
    """``extra='forbid'`` is load-bearing — typos must fail loudly.

    ADR-023 §Consequences: "An unknown field raises a typed
    ``pydantic.ValidationError`` (catch typos at startup, not at first
    use)." We exercise this directly through the model constructor (not
    via ``load_config``) so the contract is testable independent of the
    YAML loader.
    """
    with pytest.raises(ValidationError) as exc:
        # ty/mypy flag this as unknown-argument — that's *the point*:
        # the model rejects keys it doesn't declare. Silence the
        # type-checker noise so the test contract is the load-bearing signal.
        LcmConfig(unknown_knob="surprise!")  # type: ignore[call-arg]
    assert "unknown_knob" in str(exc.value)


def test_worker_config_instantiates_with_no_args() -> None:
    """``WorkerConfig`` is the placeholder for Epic 02's worker map."""
    wc = WorkerConfig()
    assert wc.model_dump() == {}


def test_worker_config_rejects_unknown_field() -> None:
    """``extra='forbid'`` is inherited by ``WorkerConfig`` too."""
    with pytest.raises(ValidationError):
        WorkerConfig(interval_s=60)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Loader — file existence, YAML shape, env interpolation
# ---------------------------------------------------------------------------


def test_missing_file_returns_default_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ``config.yaml`` is the first-run path — defaults apply.

    The operator may install lossless-hermes before populating
    ``~/.hermes/config.yaml``. The loader must NOT raise in that case;
    it falls through to env-driven defaults (post-01-02 the model has
    all fields, so we assert on the canonical default value).
    """
    # Clear env vars that would otherwise leak through resolve_lcm_config.
    for var in (
        "HERMES_CONTEXT_THRESHOLD",
        "LCM_CONTEXT_THRESHOLD",
        "HERMES_LEAF_CHUNK_TOKENS",
        "LCM_LEAF_CHUNK_TOKENS",
    ):
        monkeypatch.delenv(var, raising=False)
    missing = tmp_path / "does-not-exist.yaml"
    assert not missing.exists()
    cfg = load_config(missing)
    assert isinstance(cfg, LcmConfig)
    assert cfg.context_threshold == 0.75
    assert cfg.leaf_chunk_tokens == 20000


def test_empty_namespace_yields_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``lossless_hermes: {}`` is the explicit-but-empty configuration.

    Same observable behavior as the missing-file case, but exercises a
    different code path inside ``load_config`` (file present, YAML
    parsed, namespace present-but-empty).
    """
    for var in ("HERMES_CONTEXT_THRESHOLD", "LCM_CONTEXT_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("lossless_hermes: {}\n", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert isinstance(cfg, LcmConfig)
    assert cfg.context_threshold == 0.75
    assert cfg.enabled is True


def test_missing_namespace_yields_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``config.yaml`` without a ``lossless_hermes:`` key still loads.

    Operators may install lossless-hermes alongside other Hermes plugins
    and leave the namespace unset (using defaults). The loader treats a
    missing namespace the same as ``lossless_hermes: {}``.
    """
    for var in ("HERMES_CONTEXT_THRESHOLD", "LCM_CONTEXT_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "context:\n  engine: lcm\nplugins:\n  enabled:\n    - lossless-hermes\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert isinstance(cfg, LcmConfig)
    assert cfg.context_threshold == 0.75
    assert cfg.enabled is True


def test_unknown_key_raises_validation_error(tmp_path: Path) -> None:
    """Unknown keys under ``lossless_hermes:`` surface a typed error.

    Issue #00-07 AC + ADR-023 §Consequences: catch typos at startup
    rather than letting them silently no-op.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "lossless_hermes:\n  totally_made_up_knob: 42\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as exc:
        load_config(cfg_path)
    assert "totally_made_up_knob" in str(exc.value)


def test_env_var_interpolation_when_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``${HERMES_TEST_VAR}`` in YAML body is expanded against ``os.environ``.

    Issue #00-07 AC: "${HERMES_TEST_VAR} in YAML body is interpolated
    when the env var is set (using monkeypatch.setenv)."

    Since v0 ``LcmConfig`` has no fields, we cannot route the
    interpolated value into a Pydantic field directly. Instead we
    write the template in the *value* of a known-unknown key (Hermes's
    ``_expand_env_vars`` expands string values only — dict keys are
    left untouched). If interpolation runs, the resulting Pydantic
    ValidationError carries the expanded string in its ``input_value``
    field. If interpolation does NOT run, the error carries the literal
    template string instead. Either way, ``extra='forbid'`` triggers —
    we just inspect the input_value to distinguish.

    Once a real field lands (e.g. ``voyage_api_key`` per ADR-022), this
    same code path will hand it the expanded value before Pydantic
    sees it.
    """
    monkeypatch.setenv("HERMES_TEST_VAR", "expanded_secret_xyz")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        'lossless_hermes:\n  unknown_string_field: "${HERMES_TEST_VAR}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as exc:
        load_config(cfg_path)
    # Inspect the structured error — the input_value is what Pydantic
    # received *after* interpolation ran. If the env var was expanded,
    # it sees "expanded_secret_xyz". If not, it sees "${HERMES_TEST_VAR}".
    errors = exc.value.errors()
    assert len(errors) == 1, f"expected one validation error, got {errors}"
    assert errors[0]["input"] == "expanded_secret_xyz", (
        f"interpolation did not run — Pydantic saw {errors[0]['input']!r}"
    )


def test_env_var_interpolation_unresolved_var_is_kept_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unresolved ``${VAR}`` references are left in the YAML verbatim.

    Matches Hermes ``_expand_env_vars`` semantics (see
    ``hermes_cli.config._expand_env_vars`` — "Unresolved references
    (variable not in os.environ) are kept verbatim so callers can
    detect them"). The verbatim template surfaces in the structured
    ValidationError so an operator can diagnose the missing env var.
    """
    monkeypatch.delenv("HERMES_UNDEFINED_VAR", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        'lossless_hermes:\n  unknown_string_field: "${HERMES_UNDEFINED_VAR}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as exc:
        load_config(cfg_path)
    errors = exc.value.errors()
    assert len(errors) == 1
    # The unexpanded template stays literal because the env var is unset.
    assert errors[0]["input"] == "${HERMES_UNDEFINED_VAR}"


# ---------------------------------------------------------------------------
# Default path resolution
# ---------------------------------------------------------------------------


def test_default_path_uses_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``path`` is ``None``, the loader reads ``$HERMES_HOME/config.yaml``.

    Pin the default-path resolution contract: the loader must respect
    ``HERMES_HOME`` rather than hard-coding ``~/.hermes``. We point
    ``HERMES_HOME`` at a tmp directory, seed a ``config.yaml`` there,
    and confirm ``load_config(None)`` reads it without an explicit path.

    (When the ``tmp_home`` fixture from issue #00-04 lands in
    ``tests/conftest.py``, this test can be slimmed to use it; for now
    we inline the setup so the suite stands alone — 00-07's only
    declared dependency is #00-01.)
    """
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(
        "lossless_hermes: {}\n",
        encoding="utf-8",
    )
    cfg = load_config()
    assert isinstance(cfg, LcmConfig)
    # Post-01-02: model has defaults. Spot-check one canonical value.
    assert cfg.context_threshold == 0.75


def test_default_path_falls_back_when_hermes_home_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``HERMES_HOME`` unset ⇒ default is ``~/.hermes/config.yaml``.

    We can't safely write under the user's real home in the test, so we
    redirect ``HOME`` to a tmpdir and confirm the loader's default-path
    computation respects it (Pydantic ``Path.home()`` reads ``$HOME``).
    """
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("HERMES_CONTEXT_THRESHOLD", "LCM_CONTEXT_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    # No file at $HOME/.hermes/config.yaml — loader must fall through
    # to defaults rather than raise.
    cfg = load_config()
    assert isinstance(cfg, LcmConfig)
    # Post-01-02: model has defaults. Spot-check one canonical value.
    assert cfg.context_threshold == 0.75
