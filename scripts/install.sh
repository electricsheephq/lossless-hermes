#!/usr/bin/env bash
#
# Directory-mode installer for lossless-hermes (ADR-034, issue #134).
#
# Symlinks this checkout into ~/.hermes/plugins/lossless-hermes/ — the
# directory the Hermes `plugins` CLI scans — and installs the pinned runtime
# dependency set into the Python environment Hermes runs in. After this runs,
# `hermes plugins list` discovers the plugin and `hermes plugins enable` can
# manage it.
#
# Directory mode is the PRIMARY distribution model per ADR-034. The pip /
# entry-point path (`pip install lossless-hermes`) still works but is
# invisible to `hermes plugins list` — see the README Install section.
#
# Usage:
#   ./scripts/install.sh
#   HERMES_PROFILE=myprofile ./scripts/install.sh   # profile-scoped install
#
# Environment:
#   HERMES_HOME      Hermes home dir          (default: ~/.hermes)
#   HERMES_PROFILE   profile name; when set, installs under
#                    $HERMES_HOME/profiles/<profile>/plugins/ instead
#   PYTHON           interpreter Hermes runs under, used for the dependency
#                    install (default: the `python3` on PATH). Set this when
#                    Hermes lives in a venv — e.g.
#                    PYTHON=~/.hermes/.venv/bin/python ./scripts/install.sh
#   SKIP_DEPS=1      skip the dependency install (deps already provisioned)
#
# Activation still requires BOTH config keys (ADR-034 leaves opt-in unchanged):
#   plugins.enabled: [lossless-hermes]   and   context.engine: lcm

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- resolve the install target ------------------------------------------
HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
if [[ -n "${HERMES_PROFILE:-}" ]]; then
  TARGET_DIR="$HERMES_HOME_DIR/profiles/${HERMES_PROFILE}/plugins/lossless-hermes"
else
  TARGET_DIR="$HERMES_HOME_DIR/plugins/lossless-hermes"
fi

mkdir -p "$(dirname "$TARGET_DIR")"

# --- place the symlink (idempotent, never clobbers) ----------------------
if [[ -L "$TARGET_DIR" ]]; then
  CURRENT_TARGET="$(readlink "$TARGET_DIR")"
  if [[ "$CURRENT_TARGET" != "$REPO_ROOT" ]]; then
    echo "ERROR: refusing to replace existing symlink:" >&2
    echo "  $TARGET_DIR -> $CURRENT_TARGET" >&2
    echo "Remove it or repoint it at this checkout, then rerun install.sh." >&2
    exit 1
  fi
  echo "Symlink already points at this checkout: $TARGET_DIR"
elif [[ -e "$TARGET_DIR" ]]; then
  echo "ERROR: refusing to replace existing path: $TARGET_DIR" >&2
  echo "Move it aside or remove it, then rerun install.sh." >&2
  exit 1
else
  ln -s "$REPO_ROOT" "$TARGET_DIR"
  echo "Linked $TARGET_DIR -> $REPO_ROOT"
fi

# --- install pinned runtime dependencies ---------------------------------
# A directory plugin has no pip dependency chain (ADR-034 §Consequences), so
# the pinned set must be installed explicitly into the SAME interpreter
# Hermes runs under. The pins below are the canonical set from
# pyproject.toml [project].dependencies / docs/reference/dependencies.md
# (ADR-006). Keep this list in sync with pyproject.toml.
DEPS=(
  "httpx[socks]==0.28.1"
  "sqlite-vec==0.1.9"
  "pydantic==2.12.5"
  "pyyaml==6.0.3"
  "tenacity==9.1.4"
)

if [[ "${SKIP_DEPS:-}" == "1" ]]; then
  echo "SKIP_DEPS=1 — skipping dependency install."
else
  PYTHON_BIN="${PYTHON:-python3}"
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "ERROR: Python interpreter '$PYTHON_BIN' not found on PATH." >&2
    echo "Set PYTHON to the interpreter Hermes runs under and rerun, e.g.:" >&2
    echo "  PYTHON=~/.hermes/.venv/bin/python ./scripts/install.sh" >&2
    exit 1
  fi

  # Report the target environment so a wrong-interpreter install is visible
  # (ADR-034 §Open questions #1 — never silently install into the wrong env).
  PYTHON_RESOLVED="$("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"
  PYTHON_VERSION="$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())')"
  echo
  echo "Installing pinned dependencies into:"
  echo "  interpreter : $PYTHON_RESOLVED"
  echo "  version     : $PYTHON_VERSION"
  echo "If that is NOT the interpreter Hermes runs under, abort (Ctrl-C) and"
  echo "rerun with PYTHON set to the correct interpreter."
  echo

  if "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    "$PYTHON_BIN" -m pip install "${DEPS[@]}"
  elif command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PYTHON_BIN" "${DEPS[@]}"
  else
    echo "ERROR: neither '$PYTHON_BIN -m pip' nor 'uv' is available to install" >&2
    echo "dependencies. Install pip into the target environment, or install uv," >&2
    echo "then rerun. Required pins:" >&2
    printf '  %s\n' "${DEPS[@]}" >&2
    exit 1
  fi

  # Warn on a competing pip install of the same plugin (ADR-034 §Open
  # questions #4): a directory copy + a pip-installed entry point would load
  # the plugin twice. This is a warning, not a failure.
  if "$PYTHON_BIN" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("lossless_hermes") else 1)' >/dev/null 2>&1; then
    echo
    echo "WARNING: 'lossless_hermes' is also importable as a pip package in this" >&2
    echo "environment. Running both a directory install and a pip install of the" >&2
    echo "same plugin can load it twice. Pick one path — for directory mode," >&2
    echo "uninstall the pip copy: $PYTHON_BIN -m pip uninstall lossless-hermes" >&2
  fi
fi

# --- next steps ----------------------------------------------------------
cat <<EOF

Installed lossless-hermes (directory mode) at:
  $TARGET_DIR

Activation requires BOTH keys in ~/.hermes/config.yaml:

  plugins:
    enabled:
      - lossless-hermes

  context:
    engine: lcm

Verification:
  1. Restart Hermes.
  2. Run: hermes plugins list
     -> confirm 'lossless-hermes' appears in the list.
  3. Run: /lcm status   (inside a Hermes session)
     -> confirm the context engine is 'lcm'.
EOF
