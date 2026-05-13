"""Tests for :mod:`lossless_hermes.voyage.credentials` — the engine-init
contract per ADR-022 §Consequences.

This module exercises the *raising* resolver. The sibling pure-lookup
helper at :func:`lossless_hermes.db.config.resolve_voyage_api_key` has
its own coverage in ``tests/test_db_config.py``. We don't duplicate
precedence-ordering coverage here — both functions share the same
underlying lookup — but we do verify every test case from the issue
spec (10-1 through 10-10) against this module's contract because the
raising behavior is the load-bearing difference.

Test inventory (mirrors ``epics/05-embeddings/05-02-credentials-resolver.md``
§Tests, items 1-10):

1.  Config inline wins over env+file.
2.  Env wins over file (no config).
3.  File wins when neither config nor env set.
4.  Empty config string falls through to env.
5.  Whitespace-only env falls through to file.
6.  All three empty → ``VoyageError(kind="auth")`` with documented message.
7.  ``${VOYAGE_API_KEY}`` interpolation in config (verified via the
    plain-string case; YAML-loader interpolation lives in
    ``tests/test_config_load.py``).
8.  File doesn't exist → tier 3 skipped silently (no ``FileNotFoundError``).
9.  File exists but is whitespace-only → falls through to ``VoyageError``.
10. ``hermes_home`` override → reads from custom path.

Plus structural checks:
* The error message names every tier (so the operator can act on it).
* The function is importable from both ``voyage.credentials`` and the
  ``voyage`` package root (re-export contract).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lossless_hermes.db.config import LcmConfig
from lossless_hermes.voyage import (
    resolve_voyage_api_key as _resolve_from_package,
)
from lossless_hermes.voyage.client import VoyageError
from lossless_hermes.voyage.credentials import resolve_voyage_api_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_credentials_file(hermes_home: Path, value: str) -> Path:
    """Create ``$hermes_home/lossless-hermes/credentials/voyage-api-key``
    with the given contents and return the path."""
    cred_dir = hermes_home / "lossless-hermes" / "credentials"
    cred_dir.mkdir(parents=True, exist_ok=True)
    cred_path = cred_dir / "voyage-api-key"
    cred_path.write_text(value, encoding="utf-8")
    return cred_path


# ---------------------------------------------------------------------------
# 1. Precedence: config > env > file
# ---------------------------------------------------------------------------


class TestPrecedence:
    """ADR-022 §Consequences: strict three-tier order."""

    def test_inline_config_wins_over_env_and_file(self, tmp_path: Path) -> None:
        """Tier 1 (config inline) beats tier 2 (env) and tier 3 (file)."""
        _seed_credentials_file(tmp_path, "file-key")
        config = LcmConfig(voyage_api_key="config-key")
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "env-key"},
            hermes_home=tmp_path,
        )
        assert result == "config-key"

    def test_env_wins_over_file_when_no_config(self, tmp_path: Path) -> None:
        """Tier 2 (env) beats tier 3 (file) when tier 1 is absent."""
        _seed_credentials_file(tmp_path, "file-key")
        config = LcmConfig()  # voyage_api_key defaults to None
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "env-key"},
            hermes_home=tmp_path,
        )
        assert result == "env-key"

    def test_file_wins_when_config_and_env_empty(self, tmp_path: Path) -> None:
        """Tier 3 (file) wins when neither config nor env populated."""
        _seed_credentials_file(tmp_path, "file-key")
        config = LcmConfig()
        result = resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        assert result == "file-key"


# ---------------------------------------------------------------------------
# 2. Fall-through on empty/whitespace
# ---------------------------------------------------------------------------


class TestFallThrough:
    """Empty/whitespace at any tier must fall to the next tier — never
    "succeed" with an empty string."""

    def test_empty_config_string_falls_through_to_env(self, tmp_path: Path) -> None:
        """A literal ``""`` config value is treated as absent (tier 2 wins)."""
        config = LcmConfig(voyage_api_key="")
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "env-key"},
            hermes_home=tmp_path,
        )
        assert result == "env-key"

    def test_whitespace_only_config_falls_through_to_env(self, tmp_path: Path) -> None:
        """``"   "`` (whitespace-only) is treated as absent. Matches ``.strip()``
        semantics in :func:`_lookup_voyage_api_key`."""
        config = LcmConfig(voyage_api_key="   \t  ")
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "env-key"},
            hermes_home=tmp_path,
        )
        assert result == "env-key"

    def test_whitespace_only_env_falls_through_to_file(self, tmp_path: Path) -> None:
        """``VOYAGE_API_KEY="   "`` is treated as absent; tier 3 wins."""
        _seed_credentials_file(tmp_path, "file-key")
        config = LcmConfig()
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "   "},
            hermes_home=tmp_path,
        )
        assert result == "file-key"

    def test_trailing_newline_in_file_is_stripped(self, tmp_path: Path) -> None:
        """``echo "key" >> voyage-api-key`` leaves a trailing ``\\n`` — must
        not surface to the caller as ``"key\\n"`` (would break the
        ``Authorization: Bearer key\\n`` request header)."""
        _seed_credentials_file(tmp_path, "file-key\n")
        config = LcmConfig()
        result = resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        assert result == "file-key"

    def test_whitespace_only_file_falls_through_to_error(self, tmp_path: Path) -> None:
        """A file containing only whitespace is treated as absent. With
        config + env also empty, the resolver raises."""
        _seed_credentials_file(tmp_path, "   \n  \t\n")
        config = LcmConfig()
        with pytest.raises(VoyageError) as excinfo:
            resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        assert excinfo.value.kind == "auth"


# ---------------------------------------------------------------------------
# 3. Tier-3 file edge cases
# ---------------------------------------------------------------------------


class TestFileTier:
    """Tier-3 specifics: missing-file, directory layout, ``hermes_home``."""

    def test_file_does_not_exist_falls_through_silently(self, tmp_path: Path) -> None:
        """When the credentials file is absent, tier 3 must not raise
        :class:`FileNotFoundError`. The resolver should fall through to the
        "all tiers empty" branch and raise the standard auth error."""
        # ``tmp_path`` is empty — no credentials/ directory exists.
        config = LcmConfig()
        with pytest.raises(VoyageError) as excinfo:
            resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        assert excinfo.value.kind == "auth"

    def test_hermes_home_override_reads_custom_path(self, tmp_path: Path) -> None:
        """Explicit ``hermes_home=`` argument routes tier 3 to a custom
        path (test-only override; production engine never passes this)."""
        custom_home = tmp_path / "custom-hermes-state"
        custom_home.mkdir()
        _seed_credentials_file(custom_home, "custom-home-key")
        config = LcmConfig()
        result = resolve_voyage_api_key(
            config,
            env={},
            hermes_home=custom_home,
        )
        assert result == "custom-home-key"

    def test_hermes_home_env_var_drives_tier_3_when_no_override(self, tmp_path: Path) -> None:
        """When ``hermes_home=`` is not passed but the env mapping contains
        ``HERMES_HOME``, tier 3 derives the base from that env var. This is
        the production path: the engine passes ``env=os.environ`` and lets
        the resolver compute the default."""
        _seed_credentials_file(tmp_path, "env-driven-home-key")
        config = LcmConfig()
        result = resolve_voyage_api_key(
            config,
            env={"HERMES_HOME": str(tmp_path)},
        )
        assert result == "env-driven-home-key"


# ---------------------------------------------------------------------------
# 4. Missing-everything error contract
# ---------------------------------------------------------------------------


class TestMissingEverythingRaises:
    """Per the issue spec acceptance criteria: when every tier is empty
    the resolver MUST raise :class:`VoyageError` with ``kind="auth"`` and
    a message that names every tier the operator can populate."""

    def test_all_tiers_empty_raises_voyage_error_auth(self, tmp_path: Path) -> None:
        """Bare resolver call with empty config + empty env + missing file."""
        config = LcmConfig()
        with pytest.raises(VoyageError) as excinfo:
            resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        assert excinfo.value.kind == "auth"

    def test_error_message_names_every_tier(self, tmp_path: Path) -> None:
        """The operator-facing message must name all three remediation
        options so the operator doesn't need to consult docs to fix it.
        Matches the ADR-022 §Consequences specification verbatim."""
        config = LcmConfig()
        with pytest.raises(VoyageError) as excinfo:
            resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        message = str(excinfo.value)
        # Tier-1 mention
        assert "config.lossless_hermes.voyage_api_key" in message
        # Tier-2 mention
        assert "$VOYAGE_API_KEY" in message
        # Tier-3 mention
        assert "$HERMES_HOME/lossless-hermes/credentials/voyage-api-key" in message
        # ``voyage_auth:`` prefix — matches LCM ``client.ts`` formatting
        # style so logs/grep across the boundary continue to work.
        assert message.startswith("voyage_auth:")

    def test_error_has_no_other_voyage_error_metadata(self, tmp_path: Path) -> None:
        """``status``, ``retry_after_ms``, ``response_body`` should all be
        ``None`` — this is a *local* auth-config error, not a Voyage-HTTP
        response, so populating those would falsely imply network round-trip."""
        config = LcmConfig()
        with pytest.raises(VoyageError) as excinfo:
            resolve_voyage_api_key(config, env={}, hermes_home=tmp_path)
        assert excinfo.value.status is None
        assert excinfo.value.retry_after_ms is None
        assert excinfo.value.response_body is None


# ---------------------------------------------------------------------------
# 5. ``${VOYAGE_API_KEY}`` interpolation (already-substituted by loader)
# ---------------------------------------------------------------------------


class TestEnvInterpolation:
    """Per ADR-022 §Rationale: an operator can write ``voyage_api_key:
    "${VOYAGE_API_KEY}"`` in YAML and keep the secret out of git. The
    Hermes config loader handles the interpolation upstream, so by the
    time :class:`LcmConfig` reaches the resolver, the value is a plain
    string. We verify the plain-string path here; the loader's
    interpolation behavior is covered in ``tests/test_config_load.py``."""

    def test_substituted_config_value_is_returned_as_tier_1(self, tmp_path: Path) -> None:
        """When the loader has already substituted ``${VOYAGE_API_KEY}`` with
        the env value, the resolver sees a plain string and returns it."""
        # Simulating what the loader would produce — the value of
        # ``${VOYAGE_API_KEY}`` is in the LcmConfig as a normal string.
        config = LcmConfig(voyage_api_key="interpolated-from-env-key")
        result = resolve_voyage_api_key(
            config,
            env={"VOYAGE_API_KEY": "raw-env-key-should-not-be-used"},
            hermes_home=tmp_path,
        )
        # Tier 1 wins; the env-tier value is shadowed (the loader has
        # already promoted env into tier 1).
        assert result == "interpolated-from-env-key"


# ---------------------------------------------------------------------------
# 6. Re-export contract — caller import surface
# ---------------------------------------------------------------------------


class TestReExport:
    """The resolver must be importable from both the direct module path
    (``voyage.credentials``) and the package root (``voyage``). Engine-init
    code uses the package import; tests and library helpers may use the
    explicit submodule path."""

    def test_package_root_re_export_is_the_same_callable(self) -> None:
        """``lossless_hermes.voyage.resolve_voyage_api_key`` IS the same
        function as ``lossless_hermes.voyage.credentials.resolve_voyage_api_key``
        — not a wrapper, not a re-implementation. Catches accidental
        divergence if a future refactor adds a wrapper in __init__.py."""
        assert _resolve_from_package is resolve_voyage_api_key
