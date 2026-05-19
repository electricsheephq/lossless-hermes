"""Tests for the directory-mode plugin manifest (``plugin.yaml``).

Per [ADR-034](../../docs/adr/034-plugin-distribution-directory-mode.md),
directory-mode install is the **primary** distribution model: the plugin is
dropped under ``~/.hermes/plugins/lossless-hermes/`` and Hermes core's
directory loader reads ``plugin.yaml`` at the package root to discover it.

ADR-034 §"Invariant — the same checkout serves both modes" requires that the
``plugin.yaml`` directory manifest and the ``pyproject.toml`` pip metadata
**must not diverge**. These tests pin that invariant: the manifest is parsed
with the same ``yaml.safe_load`` Hermes core uses
(``hermes_cli/plugins.py:_parse_manifest``), and its load-bearing fields are
checked for well-formedness and consistency with ``pyproject.toml``.

What is checked:

* ``plugin.yaml`` exists at the repo root and is valid YAML (a mapping).
* ``name`` is ``lossless-hermes`` — equal to the canonical install directory
  name. For a flat directory plugin the registry *key* is the directory name;
  ``plugins.enabled`` matches on key OR name, and the README quickstart's
  allowlist entry is ``lossless-hermes``, so the manifest name must match.
* ``version`` / ``description`` / ``author`` match ``pyproject.toml``
  ``[project]`` — the no-divergence invariant.
* ``kind`` is one of Hermes core's ``_VALID_PLUGIN_KINDS`` (an unknown kind is
  silently coerced to ``standalone`` by the loader — pinning it here makes a
  typo fail loudly instead).
* ``provides_hooks`` exactly mirrors the hooks ``register()`` actually
  registers (``src/lossless_hermes/__init__.py``) and every listed hook is a
  real Hermes hook name.
* The ``[project.entry-points."hermes_agent.plugins"]`` secondary pip path is
  still declared in ``pyproject.toml`` (ADR-034 keeps it as a secondary,
  CLI-invisible path).

References:

* ``docs/adr/034-plugin-distribution-directory-mode.md`` — the decision.
* ``plugin.yaml`` — the manifest under test.
* ``hermes_cli/plugins.py`` (Hermes host) — ``_parse_manifest`` /
  ``_VALID_PLUGIN_KINDS`` / ``VALID_HOOKS``; the schema this manifest targets.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the project floor is 3.11 (ADR-005)
    pytest.skip("tomllib requires Python 3.11+", allow_module_level=True)


# tests/plugin/test_plugin_manifest.py -> repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "plugin.yaml"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"


# Hermes core's accepted plugin kinds — keep in sync with
# ``_VALID_PLUGIN_KINDS`` in ``hermes_cli/plugins.py``. An unknown ``kind`` is
# coerced to "standalone" by the loader, so a typo would otherwise pass
# silently.
_VALID_PLUGIN_KINDS = {
    "standalone",
    "backend",
    "exclusive",
    "platform",
    "model-provider",
}

# Hermes core's ``VALID_HOOKS`` set — keep in sync with ``hermes_cli/plugins.py``.
# Every name in ``provides_hooks`` must be a real Hermes lifecycle hook.
_VALID_HERMES_HOOKS = {
    "pre_tool_call",
    "post_tool_call",
    "transform_terminal_output",
    "transform_tool_result",
    "transform_llm_output",
    "pre_llm_call",
    "post_llm_call",
    "pre_api_request",
    "post_api_request",
    "on_session_start",
    "on_session_end",
    "on_session_finalize",
    "on_session_reset",
    "subagent_stop",
    "pre_gateway_dispatch",
    "pre_approval_request",
    "post_approval_response",
}

# The four hooks ``register()`` registers — see
# ``src/lossless_hermes/__init__.py``. ``provides_hooks`` in the manifest must
# mirror exactly this set (order-independent).
_REGISTERED_HOOKS = {
    "post_llm_call",
    "pre_llm_call",
    "on_session_end",
    "subagent_stop",
}


@pytest.fixture(scope="module")
def manifest() -> dict[str, object]:
    """Parse ``plugin.yaml`` the way Hermes core's ``_parse_manifest`` does.

    Hermes core uses ``yaml.safe_load(...)`` and treats a non-mapping result
    as a parse failure; this fixture mirrors that and fails the test module
    if the manifest is missing or malformed.
    """
    assert _MANIFEST_PATH.is_file(), (
        f"plugin.yaml is missing at the repo root ({_MANIFEST_PATH}). "
        "ADR-034 requires a directory-mode manifest at the package root."
    )
    data = yaml.safe_load(_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict), (
        "plugin.yaml must parse to a YAML mapping (Hermes core's "
        "_parse_manifest treats a non-mapping as a parse failure)."
    )
    return data


@pytest.fixture(scope="module")
def pyproject() -> dict[str, object]:
    """Parse ``pyproject.toml`` for cross-checking manifest metadata."""
    assert _PYPROJECT_PATH.is_file(), f"pyproject.toml missing at {_PYPROJECT_PATH}"
    with _PYPROJECT_PATH.open("rb") as fh:
        return tomllib.load(fh)


class TestManifestWellFormed:
    """``plugin.yaml`` parses and carries the load-bearing fields."""

    def test_name_is_canonical_install_directory(self, manifest: dict[str, object]) -> None:
        """``name`` equals ``lossless-hermes`` — the install directory name.

        For a flat directory plugin the registry key is the directory name;
        ``plugins.enabled`` matches key OR name. The README quickstart's
        allowlist entry is ``lossless-hermes``, so the manifest name must
        equal it (and the install directory) for the opt-in to resolve.
        """
        assert manifest.get("name") == "lossless-hermes"

    def test_kind_is_valid(self, manifest: dict[str, object]) -> None:
        """``kind`` is one of Hermes core's accepted kinds.

        An unknown ``kind`` is silently coerced to ``standalone`` by
        ``_parse_manifest`` — pinning it here makes a typo fail loudly.
        """
        kind = manifest.get("kind", "standalone")
        assert isinstance(kind, str)
        assert kind.strip().lower() in _VALID_PLUGIN_KINDS, (
            f"kind={kind!r} is not a valid Hermes plugin kind "
            f"(valid: {sorted(_VALID_PLUGIN_KINDS)})"
        )

    def test_kind_is_standalone(self, manifest: dict[str, object]) -> None:
        """lossless-hermes is a standalone context-engine plugin."""
        assert manifest.get("kind", "standalone") == "standalone"

    def test_version_is_a_string(self, manifest: dict[str, object]) -> None:
        """``version`` is present and a string.

        ``_parse_manifest`` coerces ``version`` with ``str(...)``, but an
        explicit string keeps the manifest readable and diff-stable.
        """
        assert isinstance(manifest.get("version"), str)
        assert manifest["version"].strip()


class TestManifestConsistentWithPyproject:
    """ADR-034 §Invariant — directory manifest must not diverge from pip metadata."""

    def test_version_matches_pyproject(
        self, manifest: dict[str, object], pyproject: dict[str, object]
    ) -> None:
        project = pyproject["project"]
        assert isinstance(project, dict)
        assert manifest.get("version") == project.get("version"), (
            "plugin.yaml version must match pyproject.toml [project].version "
            "(ADR-034: the same checkout serves both modes — they must not "
            "diverge)."
        )

    def test_description_matches_pyproject(
        self, manifest: dict[str, object], pyproject: dict[str, object]
    ) -> None:
        project = pyproject["project"]
        assert isinstance(project, dict)
        assert manifest.get("description") == project.get("description")

    def test_author_matches_pyproject(
        self, manifest: dict[str, object], pyproject: dict[str, object]
    ) -> None:
        """``author`` matches a ``pyproject.toml`` ``[[project.authors]]`` name."""
        project = pyproject["project"]
        assert isinstance(project, dict)
        authors = project.get("authors", [])
        assert isinstance(authors, list) and authors, "pyproject has no authors"
        author_names = {a.get("name") for a in authors if isinstance(a, dict) and a.get("name")}
        assert manifest.get("author") in author_names, (
            f"plugin.yaml author={manifest.get('author')!r} is not among "
            f"pyproject.toml author names {sorted(author_names)}"
        )


class TestManifestHooks:
    """``provides_hooks`` mirrors what ``register()`` registers."""

    def test_provides_hooks_present_and_list(self, manifest: dict[str, object]) -> None:
        hooks = manifest.get("provides_hooks")
        assert isinstance(hooks, list) and hooks, (
            "plugin.yaml must declare a non-empty provides_hooks list — the "
            "plugin registers four lifecycle hooks."
        )

    def test_provides_hooks_are_real_hermes_hooks(self, manifest: dict[str, object]) -> None:
        """Every declared hook is a real Hermes ``VALID_HOOKS`` name."""
        hooks = set(manifest.get("provides_hooks", []))
        unknown = hooks - _VALID_HERMES_HOOKS
        assert not unknown, (
            f"provides_hooks lists hook names Hermes core does not recognize: {sorted(unknown)}"
        )

    def test_provides_hooks_matches_register(self, manifest: dict[str, object]) -> None:
        """``provides_hooks`` exactly mirrors the hooks ``register()`` wires.

        ``register()`` (src/lossless_hermes/__init__.py) calls
        ``ctx.register_hook(...)`` for exactly: post_llm_call, pre_llm_call,
        on_session_end, subagent_stop. The manifest must declare exactly
        that set so ``hermes plugins`` reports the plugin's surface
        accurately.
        """
        hooks = set(manifest.get("provides_hooks", []))
        assert hooks == _REGISTERED_HOOKS, (
            "plugin.yaml provides_hooks must mirror the register_hook() calls "
            f"in src/lossless_hermes/__init__.py.\n"
            f"  manifest:  {sorted(hooks)}\n"
            f"  register(): {sorted(_REGISTERED_HOOKS)}"
        )


class TestSecondaryEntryPointStillDeclared:
    """ADR-034 keeps the pip/entry-point path as a secondary (CLI-invisible) path."""

    def test_entry_point_present_in_pyproject(self, pyproject: dict[str, object]) -> None:
        """``[project.entry-points."hermes_agent.plugins"]`` is still declared.

        ADR-034 Option B keeps the entry point so ``pip install
        lossless-hermes`` continues to work as a secondary path. Removing it
        would be Option C, which the ADR rejected.
        """
        project = pyproject.get("project", {})
        assert isinstance(project, dict)
        entry_points = project.get("entry-points", {})
        assert isinstance(entry_points, dict)
        group = entry_points.get("hermes_agent.plugins", {})
        assert isinstance(group, dict)
        assert group.get("lossless-hermes") == "lossless_hermes:register", (
            "pyproject.toml must keep the hermes_agent.plugins entry point as "
            "the secondary pip path (ADR-034 Option B)."
        )
